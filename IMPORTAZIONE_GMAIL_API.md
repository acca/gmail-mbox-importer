# Importazione MBOX con Gmail API

Lo script usa esclusivamente Gmail API, non IMAP, password per app o
GMAIL_ACCESS_TOKEN. Gli originali MBOX vengono solo letti. Nessuna dipendenza
è stata installata e nessun test, consenso OAuth o import è stato eseguito.

## Preparazione per Gmail personale

1. Nella [Google Cloud Console](https://console.cloud.google.com/) crea/seleziona
   un progetto e abilita **Gmail API** in API e servizi → Libreria.
2. Configura Google Auth Platform: Branding con nome applicazione e contatti;
   Audience **External** per Gmail personale. In modalità **Testing** aggiungi
   il tuo indirizzo Gmail fra i test users.
3. In Data Access configura lo scope
   `https://www.googleapis.com/auth/gmail.modify`: serve per importare, leggere
   i marcatori e modificare le etichette. È uno scope restricted, con accesso
   ampio alla casella. Non occorrono service account o delega di dominio.
4. In Clients crea un client OAuth **Desktop app**, scarica il JSON come
   `credentials.json` e proteggilo (`chmod 600 credentials.json`).
5. Installa localmente le dipendenze, quando pronto:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-gmail-api.txt
   ```

Al primo avvio lo script apre il browser e riceve il consenso tramite callback
localhost su una porta libera. Verifica l'account scelto: deve corrispondere
esattamente a `--user`. Il token viene scritto atomicamente in
`--state/token.json` con permessi 0600, dentro una directory 0700. È un segreto
in chiaro protetto dai permessi del filesystem: non condividerlo o versionarlo.
Il rinnovo è automatico; se il refresh viene rifiutato, archivia il solo
`token.json` e ripeti l'autorizzazione, conservando il checkpoint.

**OAuth External in Testing: il refresh token può scadere dopo sette giorni**
(con questo scope si applica la scadenza documentata da Google). Per un uso
continuativo valuta lo stato In production e i requisiti Google applicabili:
non elimina ogni possibile causa di revoca/scadenza dei token. Un'app personale
non verificata può mostrare avvisi; controlla progetto e account prima del consenso.

## Comando

```bash
.venv/bin/python importa_gmail.py \
  --user "nome@gmail.com" \
  --credentials "./credentials.json" \
  --state "./stato-importazione" \
  --source "/percorso/Archiviata.mbox" "ExportCLA/Archiviata"
```

Ripeti `--source MBOX ETICHETTA` per ogni destinazione. Vengono creati anche i
padri mancanti (`ExportCLA`, poi `ExportCLA/Archiviata`); solo la destinazione
finale viene applicata al messaggio. Tutte le destinazioni --source, staging e
quelle esplicite già nel checkpoint vengono risolte prima di caricare messaggi.
Il tipo è verificato sui metadata API: un campo type assente nella risposta di
creazione viene recuperato con labels.get, senza classificare il nome come
riservato. Solo il tipo system o un ID di sistema esatto comportano il rifiuto.
La presenza di / o una corrispondenza parziale del nome non sono criteri di rifiuto.
Un riepilogo imported indica upload confermato ma applicazione delle etichette
incompleta: alla ripresa si usa api_id per completarla, senza reinviare il messaggio.
 Resta lo staging tecnico configurabile
(default `Takeout/Importazione`). Non vengono ricostruite etichette Takeout:
`X-Gmail-Labels` viene letto solo per gli indicatori di stato.
Non vengono rimosse le etichette utente già presenti su Gmail.

## Date, flag e differenze rispetto a IMAP

`users.messages.import` invia RFC822 con `uploadType=media`, Content-Type
`message/rfc822`, senza base64 dell'intero messaggio. Il limite documentato è
150 MB: lo script usa prudenzialmente 150.000.000 byte. Rifiuti per dimensione
producono l'originale `.eml` in `--state/oversize`; `--max-mib` consente una
soglia inferiore. Gli altri rifiuti 400/422 sono registrati come `rejected`,
non reinviati automaticamente e riepilogati alla fine.

L'API non offre il parametro INTERNALDATE di APPEND. Si imposta esplicitamente
`internalDateSource=dateHeader`: per mantenere la selezione data precedente
(envelope MBOX, altrimenti Date, altrimenti epoch UTC), **nella sola copia di
upload** Date viene riscritta e l'eventuale Date precedente conservata in
X-Takeout-Original-Date. Data e fingerprint del checkpoint restano invariati.
Gmail usa Date quando valida: lo script segnala differenze di internalDate
restituita, ma non può imporre o correggere retroattivamente tale valore.
La riscrittura di Date può invalidare firme DKIM della copia importata.
I messaggi già importati e riconciliati non vengono riscritti.

Letto/non letto è rappresentato da assenza/presenza di UNREAD; la stella da
STARRED. I flag delle occorrenze duplicate si uniscono come nel checkpoint
precedente. Answered viene conservato localmente e segnalato nei log: non
esiste una corrispondente etichetta API supportata. Anche Recent, Deleted e
Draft IMAP non sono trasferiti come flag; non si crea una falsa etichetta per
rappresentarli. Nessuna rimozione di stelle già presenti.

Import applica scansione/classificazione Gmail simile alla ricezione SMTP:
non è una copia bit per bit di APPEND. Lo script richiede neverMarkSpam=true
e processForCalendar=false per evitare spam automatico e inviti nel calendario.
Gmail può comunque assegnare etichette di sistema/categorie proprie; le sole
etichette utente applicate dallo script sono --source e staging.

## Quote, retry ed errori

Documentazione ufficiale consultata il 5 novembre 2026: per i nuovi progetti
limiti di 6.000 unità/minuto per utente/progetto e 1.200.000 per progetto;
import costa 25 unità, get metadata 20, list e modify 5, labels.list e profile 1.
I progetti precedenti possono avere quote diverse: controllare la Console.
Lo script serializza le richieste a **600 unità/minuto**, con pausa minima
predefinita di 2 secondi; --units-per-minute può solo ridurre tale tetto.
Non applica il vecchio budget IMAP 350/500 MB, né il cooldown IMAP di 24 ore.
Lo storico uploads resta solo come registro dei tentativi.

**Gmail API ha comunque limiti di banda propri, indipendenti da IMAP**: non
è una garanzia di migrazione senza limitazioni. Quote temporanee terminano con
exit 75; Retry-After (secondi o data HTTP) viene conservato nel database.
Operazioni idempotenti hanno fino a sei tentativi con backoff e jitter.
Import e creazione etichette non vengono ritentati automaticamente; un rifiuto
esplicito di quota permette di riprovare import al successivo avvio, dopo la
pausa. Timeout/5xx sull'import mantengono pending. Spazio esaurito e problemi
OAuth/autorizzazioni sono distinti nei log e richiedono intervento.

## Checkpoint e riconciliazione sicura

Conserva --state, --user e --staging precedenti. Non cancellare SQLite per
sbloccare un pending. È aggiunta solo la colonna api_id: non è un UID IMAP.
Il checkpoint esistente non viene azzerato o eliminato. Un vecchio checkpoint
senza indicazione di provenienza esplicita delle etichette viene conservato e
richiede una migrazione manuale se contiene elenchi non vuoti: non si riapplicano
etichette storiche di provenienza ignota.

Non viene eseguito alcun inventario della casella, né ricerca di duplicati prima
di un nuovo upload. Si assume che i messaggi sorgenti non siano già in Gmail.
La deduplica fra MBOX usa il fingerprint e SQLite. Uno stato done con destinazioni
e flag già registrati viene saltato senza leggere Gmail; se occorre aggiungere
una destinazione, si usa il solo api_id salvato. Un done IMAP senza api_id viene
saltato se invariato; se richiede aggiornamenti lo script si ferma prima di
modificare il record, chiedendo un'associazione manuale all'ID API.

Solo per pending/imported senza ID API si effettua una ricerca **mirata** con
rfc822msgid sul Message-ID originale, includendo Spam e Cestino. Si leggono le
intestazioni di al massimo 100 candidati, verificando il marcatore esatto
X-Takeout-Import-SHA256. Non si usa mai messages.list senza query.
L'API non offre una ricerca affidabile di questo header personalizzato: senza
Message-ID utilizzabile, con troppi candidati, zero corrispondenze o marcatori
ambigui lo script si ferma chiaramente, senza scansione completa né reinvio.
Un solo candidato con marcatore esatto consente di riprendere l'applicazione
delle destinazioni. Una ricerca negativa non dimostra che l'import sia fallito.

Per continuare con gli altri messaggi quando questa verifica resta inconcludente,
aggiungi `--defer-uncertain` allo stesso comando di importazione, mantenendo tutti
gli altri argomenti. I messaggi rinviati conservano lo stato nel checkpoint:
nessun reinvio e nessuna promozione a done. Senza l'opzione, il comportamento
resta l'arresto. Errori API, autenticazione e quote continuano a fermare il lavoro.
L'opzione riguarda la riconciliazione degli import già incerti; non ignora errori
durante nuovi upload.

Il log mostra sorgente, numero del messaggio (da 1), offset in byte dell'inizio
RFC822 (dopo la riga envelope From), Message-ID, data checkpoint, Date originale,
oggetto e hash, senza corpo o allegati. Con l'opzione questi dati vengono anche
aggiunti a `--state/incerti-rinviati.jsonl`, con data di osservazione; il file è
uno storico e può contenere più occorrenze dello stesso hash. I dettagli sono
scritti progressivamente su disco senza liste di messaggi in memoria. Il
riepilogo indica il numero di occorrenze rinviate nell'esecuzione e il percorso
del report; i conteggi degli stati SQLite restano distinti. Se sono stati
rinviati messaggi, al termine della scansione l'exit code è 1. Per ritentare la
riconciliazione, ripeti il comando con le stesse sorgenti e lo stesso checkpoint.

Gli stati imported con api_id riprendono direttamente dall'ID, verificandone il
marcatore; non eseguono ricerche. Anche dopo un nuovo import si legge solo l'ID
restituito per verificare identità e data. Non sono ricerche di duplicati.
Un nuovo import salva pending prima della richiesta, poi api_id e imported
appena riceve l'ID, infine done dopo le etichette. Gli aggiornamenti di nuove
destinazioni passano a imported finché non confermati, per riprenderli dopo
un'interruzione. --retry-uncertain resta rimosso. Non cancellare il database
per sbloccare un pending. Eseguire una sola importazione alla volta per account.

## Fonti ufficiali

- [Import e limite dimensione](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/import)
- [Upload media](https://developers.google.com/workspace/gmail/api/guides/uploads)
- [InternalDateSource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/InternalDateSource)
- [Etichette di sistema](https://developers.google.com/workspace/gmail/api/guides/labels)
- [Quote correnti](https://developers.google.com/workspace/gmail/api/reference/quota)
- [Errori e limiti di banda](https://developers.google.com/workspace/gmail/api/guides/handle-errors)
- [OAuth Desktop](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Scadenza refresh token in Testing](https://developers.google.com/identity/protocols/oauth2#expiration)
