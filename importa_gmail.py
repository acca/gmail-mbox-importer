#!/usr/bin/env python3
"""
Importatore MBOX -> Gmail API per account personali.
Originali intatti; destinazioni solo da --source, oltre allo staging tecnico.
OAuth desktop e istruzioni: IMPORTAZIONE_GMAIL_API.md.
Exit: 0 completato; 1 errore/autenticazione/esito incerto; 75 quota temporanea.
Una sola esecuzione alla volta per database e account.
"""

import argparse
import csv
import email.policy
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime, format_datetime

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

HEADER = "X-Takeout-Import-SHA256"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/import"
MAX_BYTES = 150_000_000
LOG = logging.getLogger("importa")
LOCAL_HEADERS = {
    b"x-gmail-labels", b"status", b"x-status",
    b"x-mozilla-status", b"x-mozilla-status2",
    b"x-mozilla-keys", b"x-account-key",
    HEADER.lower().encode(),
}


class StopRun(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class UncertainImport(StopRun):
    """Riconciliazione inconcludente: vietato dedurre che si possa reinviare."""


def decoded(value):
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def split_message(raw):
    # mailbox.mbox.get_bytes() restituisce LF.
    normalized = raw.replace(b"\r\n", b"\n")
    head, sep, body = normalized.partition(b"\n\n")
    if not sep:
        raise ValueError("Messaggio senza separatore intestazioni/corpo")
    blocks = []
    for line in head.split(b"\n"):
        if line[:1] in (b" ", b"\t") and blocks:
            blocks[-1] += b"\n" + line
        else:
            blocks.append(line)
    return normalized, blocks, body


def iter_mbox(path):
    # Stesse frontiere di mailbox.mbox, ma senza TOC/liste di offset globali.
    # La riga vuota che precede From o EOF non appartiene al messaggio.
    # Come get_bytes(), non rimuove l'escaping >From e converte solo os.linesep;
    # la successiva normalizzazione CRLF per l'hash resta in split_message().
    with open(path, "rb") as source:
        start = None
        envelope = None
        last_empty = False
        while True:
            position = source.tell()
            line = source.readline()
            if line.startswith(b"From ") or not line:
                resume = source.tell()
                if start is not None:
                    stop = position - (len(os.linesep) if last_empty else 0)
                    source.seek(start)
                    raw = source.read(stop - start).replace(os.linesep.encode(), b"\n")
                    try:
                        yield raw, envelope, start
                    finally:
                        raw = None
                    source.seek(resume)
                if not line:
                    break
                start = resume
                envelope = line.rstrip(b"\r\n")
                last_empty = False
            else:
                last_empty = line == os.linesep.encode()


def prepare(raw):
    normalized, blocks, body = split_message(raw)
    del normalized
    digest = hashlib.sha256()
    first = True
    for block in blocks:
        if block.split(b":", 1)[0].lower() not in LOCAL_HEADERS:
            if not first:
                digest.update(b"\n")
            digest.update(block)
            first = False
    digest.update(b"\n\n")
    digest.update(body)
    fingerprint = digest.hexdigest()
    upload_blocks = [
        b for b in blocks
        if b.split(b":", 1)[0].lower() != HEADER.lower().encode()
    ]
    upload_blocks.insert(0, HEADER.encode() + b": " + fingerprint.encode())
    # Solo intestazioni al parser, mai il corpo MIME.
    headers = BytesHeaderParser(policy=email.policy.default).parsebytes(
        b"\n".join(blocks) + b"\n\n"
    )
    return fingerprint, upload_blocks, body, headers


def wire_chunks(blocks, body):
    yield (b"\n".join(blocks) + b"\n\n").replace(b"\n", b"\r\n")
    for offset in range(0, len(body), 1024 * 1024):
        yield body[offset:offset + 1024 * 1024].replace(b"\n", b"\r\n")


def wire_size(blocks, body):
    head = b"\n".join(blocks) + b"\n\n"
    return len(head) + head.count(b"\n") + len(body) + body.count(b"\n")


def metadata(headers, envelope, source_label):
    # Legge gli indicatori locali esclusivamente per ricostruire i flag supportati (Answered solo nel checkpoint).
    flag_indicators = set()
    for value in headers.get_all("X-Gmail-Labels", []):
        value = decoded(value).replace("\r", "").replace("\n", "")
        # Takeout non definisce una codifica universale e non ambigua
        # per nomi che contengono virgole. csv gestisce quelli quotati.
        flag_indicators.update(
            item.strip().casefold()
            for item in next(csv.reader([value], skipinitialspace=True))
            if item.strip()
        )

    lower = flag_indicators
    flags = set()
    status = str(headers.get("Status", "")).upper()
    xstatus = str(headers.get("X-Status", "")).upper()

    if "R" in status or lower & {"read", "\\seen"}:
        flags.add("\\Seen")
    if "A" in xstatus or lower & {"answered", "replied", "\\answered"}:
        flags.add("\\Answered")
    if "F" in xstatus or lower & {"starred", "\\starred"}:
        flags.add("\\Flagged")

    try:
        moz = int(str(headers.get("X-Mozilla-Status", "0")).strip(), 16)
        if moz & 0x0001:
            flags.add("\\Seen")
        if moz & 0x0002:
            flags.add("\\Answered")
        if moz & 0x0004:
            flags.add("\\Flagged")
    except ValueError:
        pass

    # Unread esplicito prevale sugli altri indicatori locali.
    if lower & {"unread", "\\unread"}:
        flags.discard("\\Seen")

    labels = {source_label}

    dt = None
    if envelope:
        try:
            # "From mittente Tue Jan 01 12:34:56 2019"
            text = envelope.decode("ascii", "replace")
            parts = text.split(maxsplit=2)
            dt = parsedate_to_datetime(parts[2])
        except (ValueError, TypeError, IndexError, OverflowError):
            pass
    if dt is None:
        try:
            dt = parsedate_to_datetime(str(headers.get("Date", "")))
        except (ValueError, TypeError, OverflowError):
            pass
    if dt is None:
        # Riproducibile: non assegna la data dell'esecuzione.
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
        LOG.warning("Data assente/non valida: uso 1970-01-01")
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
        LOG.warning("Data senza fuso: assumo UTC")

    return labels, flags, dt


def save_token(path, credentials):
    # File atomico 0600 nella directory di stato 0700; mai nei log.
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(credentials.to_json())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def refresh_credentials(credentials):
    with requests.Session() as session:
        credentials.refresh(Request(session=session))


def authorize(args):
    path = Path(args.state) / "token.json"
    creds = None
    if path.exists():
        os.chmod(path, 0o600)
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not creds.has_scopes(SCOPES):
            raise StopRun("Token privo di gmail.modify: archivia token.json e riautorizza")
    if creds is None:
        flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)
        try:
            creds = flow.run_local_server(
                host="localhost", port=0, open_browser=True,
                access_type="offline", prompt="consent"
            )
        finally:
            flow.oauth2session.close()
    elif not creds.valid:
        try:
            refresh_credentials(creds)
        except RefreshError:
            raise StopRun("Refresh OAuth fallito: riautorizzare archiviando token.json") from None
    save_token(path, creds)
    return creds


class APIError(Exception):
    def __init__(self, response):
        self.status = response.status_code
        try:
            error = response.json().get("error", {})
            self.reasons = {e.get("reason", "") for e in error.get("errors", [])}
            self.message = error.get("message", "Errore API")
        except (ValueError, AttributeError):
            self.reasons, self.message = set(), "Risposta API non JSON"
        retry = response.headers.get("Retry-After", "0")
        try:
            self.retry_after = max(0, float(retry))
        except ValueError:
            try:
                self.retry_after = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                self.retry_after = 0
        text = (" ".join(self.reasons) + " " + self.message).lower()
        self.storage = any(x in text for x in (
            "storagequotaexceeded", "storage quota", "storage full", "insufficient storage"
        ))
        self.size = self.status == 413 or any(x in text for x in (
            "message too large", "message size exceeds", "maximum message size",
            "messagetoolarge", "too big"
        ))
        self.temporary = not self.storage and (
            self.status == 429 or self.status >= 500 or bool(self.reasons & {
                "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
                "quotaExceeded", "backendError"
            })
        )
        super().__init__(f"HTTP {self.status}: {self.message}")


class Importer:
    def __init__(self, args, db, credentials):
        self.a, self.db, self.credentials = args, db, credentials
        # Niente refresh/retry automatici su POST potenzialmente già accettate.
        self.client = AuthorizedSession(credentials, max_refresh_attempts=0)
        self.next_call = 0
        self.labels = {}
        self.resolved_labels = {}
        self.system_label_ids = set()

    def close(self):
        self.client.close()

    def request(self, method, url, cost=5, safe=True, **kwargs):
        for attempt in range(6):
            cooldown = self.db.execute(
                "SELECT value FROM config WHERE key='api_retry_after'"
            ).fetchone()
            if cooldown and float(cooldown[0]) > time.time():
                raise StopRun("Attesa Retry-After API ancora attiva; riprovare più tardi", 75)
            wait = self.next_call - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_call = time.monotonic() + max(
                self.a.pause, cost * 60 / self.a.units_per_minute
            )
            if not self.credentials.valid:
                try:
                    refresh_credentials(self.credentials)
                except RefreshError:
                    raise StopRun("Rinnovo OAuth fallito: riautorizzare token.json") from None
                save_token(Path(self.a.state) / "token.json", self.credentials)
            try:
                with self.client.request(method, url, timeout=(30, 180), **kwargs) as response:
                    if response.ok:
                        return response.json()
                    exc = APIError(response)
                del response
                if exc.retry_after:
                    with self.db:
                        self.db.execute(
                            "INSERT OR REPLACE INTO config VALUES ('api_retry_after',?)",
                            (str(time.time() + exc.retry_after),)
                        )
                if not safe or not exc.temporary:
                    raise exc
            except requests.RequestException:
                if not safe or attempt == 5:
                    raise
                exc = None
            if attempt == 5:
                raise StopRun("Errore temporaneo API: riprovare più tardi", 75)
            delay = max(min(64, 2 ** attempt) + random.uniform(0, 1),
                        exc.retry_after if exc else 0)
            if delay > 60:
                raise StopRun("Quota API: rispettare Retry-After e riprovare più tardi", 75)
            LOG.warning("Retry operazione idempotente fra %.1fs", delay)
            time.sleep(delay)
        raise StopRun("Tentativi API esauriti", 75)

    def connect(self):
        profile = self.request("GET", API + "/profile", cost=1)
        if profile["emailAddress"].casefold() != self.a.user.casefold():
            raise StopRun("Account OAuth diverso da --user; nessuna importazione eseguita")
        self.labels = {x["name"]: x for x in self.request(
            "GET", API + "/labels", cost=1
        ).get("labels", [])}
        self.system_label_ids = {
            label["id"] for label in self.labels.values() if label.get("type") == "system"
        }
        LOG.info("Avvio senza scansione della casella; deduplica tramite checkpoint locale")

    def reconcile_uncertain(self, fingerprint, headers):
        # Gmail non supporta una ricerca affidabile per X-Takeout-Import-SHA256.
        # Restringe i candidati al Message-ID originale e verifica il marcatore
        # leggendo solo quei candidati. Mai usare una lista senza query.
        values = headers.get_all("Message-ID", [])
        message_id = str(values[0]).strip() if len(values) == 1 else ""
        if message_id.startswith("<") and message_id.endswith(">"):
            message_id = message_id[1:-1]
        if not message_id or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@:-]+", message_id):
            raise UncertainImport(
                f"{fingerprint}: import incerto senza Message-ID ricercabile. "
                "Il marcatore personalizzato non è ricercabile in modo affidabile via API; "
                "serve verifica manuale. Nessuna scansione o reinvio."
            )
        # Quota il valore: caratteri ammessi nell'ID (per esempio { e })
        # non devono diventare operatori della query Gmail. params cura l'URL encoding.
        query = 'rfc822msgid:"' + message_id + '"'
        LOG.info("Verifica mirata import incerto %s tramite Message-ID", fingerprint)
        page, examined, matches = None, 0, set()
        while True:
            params = {"q": query,
                      "includeSpamTrash": "true", "maxResults": 100}
            if page:
                params["pageToken"] = page
            result = self.request("GET", API + "/messages", params=params)
            for item in result.get("messages", []):
                examined += 1
                if examined > 100:
                    raise UncertainImport(f"{fingerprint}: troppi candidati per la verifica mirata; nessun reinvio")
                message = self.get_message(item["id"])
                markers = [x["value"].strip() for x in message.get("payload", {}).get("headers", [])
                           if x["name"].casefold() == HEADER.casefold()]
                if fingerprint in markers:
                    if markers != [fingerprint]:
                        raise UncertainImport(f"{fingerprint}: marcatori ambigui; verifica manuale")
                    matches.add(item["id"])
            page = result.get("nextPageToken")
            if not page:
                break
            if examined >= 100:
                raise UncertainImport(f"{fingerprint}: verifica mirata oltre 100 candidati; nessun reinvio")
        if len(matches) != 1:
            raise UncertainImport(
                f"{fingerprint}: verifica mirata inconcludente ({len(matches)} corrispondenze). "
                "L'assenza dai risultati non prova il mancato import. "
                "Attendere e riprendere oppure verificare manualmente; nessun reinvio."
            )
        return next(iter(matches))

    def get_message(self, message_id):
        return self.request("GET", API + "/messages/" + message_id, cost=20,
                            params={"format": "metadata", "metadataHeaders": HEADER})

    def ensure_folder(self, name):
        if (not isinstance(name, str) or not name.strip()
                or any(not part.strip() for part in name.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise StopRun(f"Nome destinazione non valido: {name!r}")
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            if folder in self.resolved_labels:
                continue
            if folder not in self.labels:
                # Create non viene ritentato: al prossimo avvio labels.list riconcilia.
                label = self.request(
                    "POST", API + "/labels", safe=False,
                    json={"name": folder, "labelListVisibility": "labelShow",
                          "messageListVisibility": "show"}
                )
                # Una risposta create può non includere type. La sua assenza
                # non significa system: crea soltanto etichette personalizzate.
                self.labels[folder] = label
            label = self.labels[folder]
            label_id = label.get("id")
            if not isinstance(label_id, str) or not label_id:
                raise StopRun(f"ID etichetta mancante per {folder}; nessun upload")
            if label.get("type") not in ("user", "system"):
                # Risolve metadata incompleti senza dedurre il tipo dal nome.
                label = self.request("GET", API + "/labels/" + label_id, cost=1)
                self.labels[folder] = label
                if label.get("id") != label_id or label.get("name") != folder:
                    raise StopRun(f"Metadata etichetta incoerenti per {folder}; nessun upload")
            # Solo tipo system esplicito o identità esatta di un ID di sistema.
            # / indica gerarchia e non rende riservato alcun nome personalizzato.
            if label.get("type") == "system" or label_id in self.system_label_ids:
                raise StopRun(f"Destinazione riservata Gmail: {folder} (ID {label_id})")
            if label.get("type") != "user":
                raise StopRun(f"Tipo etichetta non determinabile per {folder}; nessun upload")
            self.resolved_labels[folder] = label_id
        return self.resolved_labels[name]

    def prepare_destinations(self, sources):
        # Solo configurazione e SQLite: nessuna scansione dei messaggi Gmail.
        destinations = {self.a.staging} | {label for _, label in sources}
        for (saved_labels,) in self.db.execute("SELECT DISTINCT labels FROM messages"):
            labels = json.loads(saved_labels)
            if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels):
                raise StopRun("Destinazioni checkpoint non valide; database conservato")
            destinations.update(labels)
        for name in sorted(destinations):
            label_id = self.ensure_folder(name)
            LOG.info("Destinazione risolta %s -> %s", name, label_id)
        LOG.info("Tutte le destinazioni risolte prima degli upload")

    def export_eml(self, fingerprint, raw, reason):
        target = Path(self.a.state) / "oversize"
        target.mkdir(exist_ok=True)
        dest = target / (fingerprint + ".eml")
        if not dest.exists():
            tmp = dest.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='oversize', error=? WHERE hash=?",
                (reason, fingerprint)
            )
        LOG.warning("EML %s: %s", dest.name, reason)

    def process(self, raw, envelope, source_label, source, index, offset):
        with ExitStack() as resources:
            return self._process(raw, envelope, source_label, resources, source, index, offset)

    def _process(self, raw, envelope, source_label, resources, source, index, offset):
        h, blocks, body, headers = prepare(raw)  # Fingerprint identico al vecchio IMAP.
        wire_length = wire_size(blocks, body)
        labels, flags, dt = metadata(headers, envelope, source_label)
        row = self.db.execute(
            "SELECT status,labels,flags,date,api_id FROM messages WHERE hash=?", (h,)
        ).fetchone()
        state, api_id = "new", None
        if row:
            state, old_labels, old_flags, saved_date, api_id = row
            labels |= set(json.loads(old_labels))
            flags |= set(json.loads(old_flags))
            dt = datetime.fromisoformat(saved_date)
            # Un done è confermato dal checkpoint: nessuna lettura Gmail.
            if (state == "done" and labels == set(json.loads(old_labels))
                    and flags == set(json.loads(old_flags))):
                LOG.debug("Già importato, salto %s", h)
                return
            if state == "done":
                if not api_id:
                    raise StopRun(
                        f"{h}: import IMAP confermato senza ID API; nuove destinazioni/flag "
                        "richiedono associazione manuale. Checkpoint conservato, nessun reinvio."
                    )
                # Aggiornamenti non ancora confermati: non saltarli dopo un crash.
                state = "imported"
        with self.db:
            self.db.execute("""
                INSERT INTO messages(hash,status,labels,flags,date,error)
                VALUES (?,?,?,?,?,NULL)
                ON CONFLICT(hash) DO UPDATE SET status=excluded.status,
                  labels=excluded.labels, flags=excluded.flags
            """, (h, state, json.dumps(sorted(labels)), json.dumps(sorted(flags)), dt.isoformat()))
        if state in ("oversize", "rejected"):
            return

        message_id = api_id
        if state == "imported" and message_id:
            LOG.info("Ripresa etichettatura %s ID=%s: nessun nuovo upload", h, message_id)
        if message_id is None and state == "done":
            # Vecchio done IMAP: import confermato, ma nessun ID API disponibile
            # per aggiungere nuove destinazioni. Non cercare né reinviare.
            raise StopRun(
                f"{h}: import confermato nel checkpoint IMAP, ma senza ID API per "
                "aggiornare destinazioni/flag. Serve associazione manuale; nessun reinvio."
            )
        if message_id is None and state in ("pending", "imported"):
            try:
                message_id = self.reconcile_uncertain(h, headers)
            except UncertainImport as exc:
                details = {
                    "source": str(source), "position": index, "rfc822_offset": offset,
                    "message_ids": [str(value) for value in headers.get_all("Message-ID", [])],
                    "date": dt.isoformat(), "header_date": str(headers.get("Date", "")),
                    "subject": decoded(headers.get("Subject", "")), "hash": h,
                    "status": state, "reason": str(exc),
                }
                LOG.warning("Import incerto, nessun reinvio: %s", json.dumps(details, ensure_ascii=True))
                if not self.a.defer_uncertain:
                    raise
                # Report su disco, senza accumulare intestazioni o hash in RAM.
                report = Path(self.a.state) / "incerti-rinviati.jsonl"
                with open(report, "a", encoding="utf-8") as output:
                    output.write(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(),
                                             **details}, ensure_ascii=True) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                LOG.warning("Rinviato %s: stato %s conservato; dettagli in %s", h, state, report)
                return True
            with self.db:
                self.db.execute("UPDATE messages SET api_id=?,status='imported' WHERE hash=?", (message_id, h))
        if message_id is None:
            # L'API non accetta INTERNALDATE arbitrario come APPEND IMAP.
            # Date della sola copia di upload viene allineata alla data salvata
            # (envelope MBOX, poi Date, poi epoch). Originali ed hash restano intatti.
            date_blocks = [b for b in blocks if b.split(b":", 1)[0].lower() == b"date"]
            blocks = [b for b in blocks if b.split(b":", 1)[0].lower() != b"date"]
            for block in date_blocks:
                blocks.append(b"X-Takeout-Original-Date:" + block.split(b":", 1)[1])
            blocks.append(b"Date: " + format_datetime(dt).encode("ascii"))
            wire_length = wire_size(blocks, body)
            if wire_length > MAX_BYTES or (self.a.max_mib and wire_length > self.a.max_mib * 1024**2):
                self.export_eml(h, raw, "Dimensione oltre il limite API/configurato")
                return
            wire = resources.enter_context(tempfile.TemporaryFile(mode="w+b"))
            for chunk in wire_chunks(blocks, body):
                wire.write(chunk)
            del chunk
            wire.seek(0)
            # Il corpo non serve più: EML usa ancora i byte originali.
            del body, blocks
            with self.db:
                self.db.execute("UPDATE messages SET status='pending',error=NULL WHERE hash=?", (h,))
                self.db.execute("INSERT INTO uploads(ts,bytes) VALUES (?,?)", (time.time(), wire_length))
            try:
                # Media binario RFC822, niente base64 globale né retry automatico.
                result = self.request(
                    "POST", UPLOAD, cost=25, safe=False,
                    params={"uploadType": "media", "internalDateSource": "dateHeader",
                            "neverMarkSpam": "true", "processForCalendar": "false"},
                    headers={"Content-Type": "message/rfc822"}, data=wire
                )
                message_id = result.get("id")
                if not message_id:
                    raise StopRun("Import senza ID: esito incerto, pending conservato")
                # Conferma durevole appena l'ID è disponibile, prima del cleanup.
                with self.db:
                    self.db.execute("UPDATE messages SET api_id=?,status='imported' WHERE hash=?", (message_id, h))
                wire.close()
            except APIError as exc:
                with self.db:
                    self.db.execute("UPDATE messages SET error=? WHERE hash=?", (str(exc), h))
                if exc.size:
                    self.export_eml(h, raw, str(exc))
                    return
                # 4xx espliciti sono rifiuti; timeout/5xx rimangono pending.
                if 400 <= exc.status < 500 and exc.status != 408:
                    next_state = "rejected" if exc.status in (400, 422) and not exc.storage else "new"
                    with self.db:
                        self.db.execute("UPDATE messages SET status=? WHERE hash=?", (next_state, h))
                    if next_state == "rejected":
                        LOG.error("Messaggio rifiutato %s: %s", h, exc)
                        return
                if exc.storage:
                    raise StopRun("Spazio Gmail esaurito: liberare spazio o aumentare capacità") from None
                if exc.status == 401:
                    raise StopRun("Autenticazione API rifiutata: riautorizzare OAuth") from None
                if exc.temporary:
                    with self.db:
                        self.db.execute(
                            "INSERT OR REPLACE INTO config VALUES ('api_retry_after',?)",
                            (str(time.time() + max(exc.retry_after, 60 + random.uniform(0, 5))),)
                        )
                    raise StopRun("Quota/errore temporaneo API; riprendere più tardi, senza reinvio cieco", 75) from None
                raise
            except requests.RequestException:
                raise StopRun(f"Import {h} incerto: pending conservato, riprendere per riconciliare") from None

        # Lettura del solo ID restituito dall'import o salvato nel checkpoint:
        # verifica d'identità, non ricerca di duplicati nella casella.
        message = self.get_message(message_id)
        markers = [x["value"].strip() for x in message.get("payload", {}).get("headers", [])
                   if x["name"].casefold() == HEADER.casefold()]
        if markers != [h]:
            raise StopRun(f"Marcatore API non univoco per {h}; nessuna modifica")
        if message.get("internalDate") and abs(int(message["internalDate"]) / 1000 - dt.timestamp()) > 1:
            LOG.warning("%s: internalDate Gmail differisce dalla data checkpoint (%s)", h, dt.isoformat())
        # Mapping già validato prima di qualsiasi upload; si applica solo
        # l'ID della destinazione finale, non gli ID dei padri intermedi.
        add = {self.resolved_labels[label] for label in labels}
        add.add(self.resolved_labels[self.a.staging])
        remove = []
        if "\\Seen" in flags:
            remove.append("UNREAD")
        else:
            add.add("UNREAD")
        if "\\Flagged" in flags:
            add.add("STARRED")
        if "\\Answered" in flags:
            LOG.info("%s: flag Answered conservato solo nel checkpoint, non supportato da API", h)
        self.request("POST", API + "/messages/" + message_id + "/modify",
                     json={"addLabelIds": sorted(add), "removeLabelIds": remove})
        with self.db:
            self.db.execute("UPDATE messages SET status='done',api_id=?,error=NULL WHERE hash=?", (message_id, h))
        LOG.info("OK %s bytes=%d destinazioni=%d", h, wire_length, len(labels))


def main():
    with ExitStack() as resources:
        run(resources)


def run(resources):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", required=True)
    p.add_argument(
        "--source", action="append", nargs=2, required=True,
        metavar=("MBOX", "ETICHETTA"),
        help="Destinazione esplicita; / crea padre e sottoetichetta. Ripetibile"
    )
    p.add_argument("--state", default="./stato-importazione")
    p.add_argument("--staging", default="Takeout/Importazione")
    p.add_argument("--credentials", required=True, help="JSON client OAuth Desktop Google")
    p.add_argument("--units-per-minute", type=float, default=600,
                   help="Budget API seriale: 1..600 unità/min (default 600)")
    p.add_argument("--pause", type=float, default=2)
    p.add_argument(
        "--defer-uncertain", action="store_true",
        help="Rinvia gli import preesistenti con riconciliazione inconcludente, senza reinviarli"
    )
    p.add_argument(
        "--max-mib", type=float, default=0,
        help="0: prova il server; >0: esporta preventivamente oltre la soglia"
    )
    a = p.parse_args()
    if not 0 < a.units_per_minute <= 600 or a.pause < 0 or a.max_mib < 0:
        p.error("Valori numerici non validi")
    if not Path(a.credentials).expanduser().is_file():
        p.error("File credenziali OAuth Desktop non trovato")
    a.credentials = str(Path(a.credentials).expanduser().resolve())
    os.umask(0o077)

    # Controlla tutte le sorgenti prima di collegarsi.
    sources = []
    for filename, label in a.source:
        path = Path(filename).expanduser().resolve(strict=True)
        if (not path.is_file() or not label.strip()
                or any(not part.strip() for part in label.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            p.error(f"Sorgente/etichetta non valida: {filename}")
        sources.append((path, label))

    state = Path(a.state).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    a.state = str(state)
    os.chmod(state, 0o700)

    lock = resources.enter_context(open(state / "run.lock", "a"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise StopRun("Un'altra esecuzione usa questo database")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(state / "importazione.log", encoding="utf-8")
        ]
    )
    db = sqlite3.connect(state / "checkpoint.sqlite")
    resources.callback(db.close)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
            hash TEXT PRIMARY KEY, status TEXT NOT NULL,
            labels TEXT NOT NULL, flags TEXT NOT NULL,
            date TEXT NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS uploads (
            ts REAL NOT NULL, bytes INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS uploads_ts ON uploads(ts);
    """)

    # Migrazione additiva: gli ID API non sono UID IMAP.
    if "api_id" not in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
        with db:
            db.execute("ALTER TABLE messages ADD COLUMN api_id TEXT")

    identity = json.dumps(
        {"user": a.user.casefold(), "staging": a.staging}, sort_keys=True
    )
    existing = db.execute(
        "SELECT value FROM config WHERE key='identity'"
    ).fetchone()
    if existing and existing[0] != identity:
        raise StopRun("Database associato a un altro account/etichetta tecnica")
    with db:
        db.execute(
            "INSERT OR IGNORE INTO config VALUES ('identity',?)", (identity,)
        )

    # Non azzerare il checkpoint. Un vecchio elenco di provenienza ignota
    # non può essere riapplicato come se contenesse sole destinazioni esplicite.
    if not db.execute(
        "SELECT 1 FROM config WHERE key='explicit_labels_v1'"
    ).fetchone():
        if db.execute("SELECT 1 FROM messages WHERE labels != '[]' LIMIT 1").fetchone():
            raise StopRun(
                "Checkpoint precedente alle destinazioni esplicite: etichette di "
                "provenienza ignota. Serve migrazione manuale degli elenchi; "
                "checkpoint conservato, nessuna scansione o importazione."
            )
        with db:
            db.execute("INSERT INTO config VALUES ('explicit_labels_v1', '1')")

    # Il cooldown IMAP e uploads storici sono conservati, ma non limitano l'API.

    for path, _ in sources:
        st = path.stat()
        previous = db.execute(
            "SELECT size,mtime_ns FROM sources WHERE path=?", (str(path),)
        ).fetchone()
        if previous and previous != (st.st_size, st.st_mtime_ns):
            raise StopRun(f"MBOX cambiata dal primo avvio: {path}")
        with db:
            db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?,?,?)",
                (str(path), st.st_size, st.st_mtime_ns)
            )

    importer = Importer(a, db, authorize(a))
    resources.callback(importer.close)
    deferred = 0
    try:
        importer.connect()
        importer.prepare_destinations(sources)
        for path, label in sources:
            LOG.info("Scansione %s -> %s", path, label)
            # Lettura sequenziale, nessuna scrittura sugli originali.
            with ExitStack() as mbox_resources:
                messages = iter_mbox(path)
                mbox_resources.callback(messages.close)
                for index, (raw, envelope, offset) in enumerate(messages, 1):
                    try:
                        if importer.process(raw, envelope, label, path, index, offset):
                            deferred += 1
                    except Exception:
                        LOG.error("Sorgente=%s messaggio=%d", path, index)
                        raise
                    finally:
                        raw = envelope = None
                    if index % 500 == 0:
                        LOG.info("Esaminati %d messaggi in %s", index, path.name)
        LOG.info("Scansione completata")
    finally:
        LOG.info(
            "Etichette: solo destinazioni --source e staging tecnico; "
            "le etichette utente già presenti su Gmail non vengono rimosse; "
            "Answered non rappresentabile nell’API"
        )
        for status, count in db.execute(
            "SELECT status,COUNT(*) FROM messages GROUP BY status"
        ):
            LOG.info("Riepilogo %s=%d", status, count)
        if deferred:
            LOG.warning(
                "Riepilogo incerti rinviati: %d occorrenze, non completate e non reinviate. "
                "Dettagli per verifica manuale: %s (storico append-only); checkpoint conservato",
                deferred, state / "incerti-rinviati.jsonl"
            )
    if deferred:
        raise StopRun("Scansione terminata con import incerti rinviati; riprendere per riconciliare", 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrotto. Riprendi con lo stesso comando.", file=sys.stderr)
        sys.exit(130)
    except StopRun as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)
    except APIError as exc:
        if exc.storage:
            logging.error("Spazio Gmail esaurito: %s", exc)
        elif exc.status == 401:
            logging.error("Autenticazione OAuth rifiutata: riautorizzare token.json")
        elif exc.temporary:
            logging.error("Quota/errore temporaneo API: %s", exc)
        else:
            logging.error("Errore API/autorizzazioni: %s", exc)
        sys.exit(75 if exc.temporary else 1)
    except Exception:
        logging.exception("Importazione interrotta; checkpoint conservato")
        sys.exit(1)