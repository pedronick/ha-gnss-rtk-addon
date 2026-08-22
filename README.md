# RTK Base Station — Add-on Home Assistant (Unicore / u-blox)

Add-on Supervisor (non un'integrazione HACS in senso stretto — vedi nota
sotto), nato per l'Unicore UM982 ma **strutturato con un driver per
ricevitore** (vedi sezione "Supporto multi-ricevitore" più sotto) — supporta
oggi anche u-blox ZED-F9P/M8P, e ne può supportare altri con un nuovo
modulo in `drivers/`. Cosa fa:
- legge il ricevitore GNSS RTK configurato (`receiver_type`) via seriale;
- invia le correzioni RTCM come **server NTRIP** a uno o più caster tramite
  RTKLIB `str2str` (stesso ruolo dei servizi `str2str_ntrip_A/B` di
  RTKBase);
- espone in Home Assistant via **MQTT Discovery**:
  - `sensor.fix_status` — Single / DGPS / Float / Fix / ecc.
  - `sensor.satelliti_in_uso`
  - `sensor.accuratezza_stimata` (metri, da NMEA GST)
  - `sensor.survey_in` — idle / running / done / error / **cancelled** (media rapida, metrica)
  - `button.avvia_survey_in` / `button.annulla_survey_in`
  - `sensor.survey_in_tempo_rimanente` (secondi, aggiornato ogni secondo mentre è in corso)
  - `sensor.campagna_ppp` — idle / logging / processing / done / error / **cancelled**
  - `number.durata_campagna_ppp` (ore)
  - `button.avvia_campagna_ppp` / `button.annulla_campagna_ppp` — elaborazione PPP-static su log raw di più ore (centimetrica)
  - `sensor.campagna_ppp_tempo_rimanente` (secondi, aggiornato ogni secondo durante la fase di registrazione)
  - `number.latitudine_manuale` / `longitudine_manuale` / `altezza_manuale`
  - `button.applica_posizione_manuale`
  - `sensor.rover_connessi_caster_locale` (solo se `caster_enabled: true`)
  - `binary_sensor.dispositivo_connesso` — ON/OFF, utile per automazioni/notifiche
  - `sensor.ultimo_dato_ricevuto` (timestamp dell'ultimo dato NMEA valido)
  - `sensor.rtcm_bitrate_in_ingresso` (bps, dallo stato reale di `str2str`)
  - `sensor.stato_uscita_1/2/3/...` (uno per ogni caster/log/relay locale
    configurato: Connesso / In attesa / Chiuso / Errore, letto dallo stato
    reale di `str2str`, non solo dal fatto che il processo sia vivo)
  - `sensor.diagnostica_str2str` (ultimo messaggio di errore/stato dai
    singoli output, es. una password errata sul caster)
  - `sensor.errore_di_configurazione` (es. troppi output configurati, vedi
    sotto)
  - `sensor.ultima_posizione_calcolata` (timestamp + attributi con
    lat/lon/height/metodo/parametri, vedi sotto)
- opzionalmente, fa da **NTRIP Caster locale** (porta 2101/tcp) a cui i rover
  possono collegarsi direttamente, in aggiunta o al posto dei caster esterni

Espone inoltre un **pannello grafico nella sidebar di Home Assistant**
(via Ingress) con uno skyplot in tempo reale: satelliti visibili
posizionati per azimuth/elevazione, colorati in verde se usati nel fix
corrente, badge con stato del fix, satelliti in uso, accuratezza stimata,
HDOP/PDOP. Nessuna configurazione aggiuntiva richiesta: appare
automaticamente nella sidebar dopo l'installazione ("RTK Base").

## Supporto multi-ricevitore

Tutto il resto della pipeline (RTKLIB `str2str`, parsing NMEA per
fix/satelliti/skyplot, caster NTRIP locale, campagna PPP) è già generico:
RTCM3 e NMEA-0183 sono protocolli standard, non specifici di un
produttore. L'unica parte davvero dipendente dal ricevitore è **come lo si
configura** (abilitare i messaggi giusti, impostare la posizione base
fissa) — isolata nel package `drivers/`:

- `drivers/base.py` — documenta il contratto: ogni driver è un modulo che
  espone `configure_rtcm(port, baud)`, `configure_nmea(port, baud)`,
  `set_rover_mode(port, baud)`, `set_fixed_base(port, baud, lat, lon, height)`.
- `drivers/unicore.py` — driver per Unicore UM980/UM982 (comandi ASCII
  `log ... ontime ...` / `mode base|rover`).
- `drivers/ublox.py` — driver per u-blox ZED-F9P/F9R/M8P (protocollo
  binario UBX: `UBX-CFG-MSG` per abilitare i messaggi, `UBX-CFG-TMODE3`
  per rover/base fissa).
- `drivers/__init__.py` — registro; l'opzione `receiver_type` dell'add-on
  seleziona quale driver usare (`unicore_um98x` o `ublox_zedf9p`).

**Per aggiungere un altro ricevitore** (es. Septentrio Mosaic-X5): crea
`drivers/nuovo_modulo.py` con le stesse quattro funzioni, registralo in
`drivers/__init__.py` (`DRIVERS["nuovo_modulo"] = nuovo_modulo`), e
aggiungi il valore all'enum `receiver_type` in `config.yaml`. Nessuna
modifica richiesta al resto del codice (`main.py`, `nmea.py`, `ppp.py`,
`caster.py` restano invariati).

Il driver u-blox è stato validato solo a livello di codice: framing
UBX/checksum testato con round-trip numerico (lat/lon/altezza codificate e
decodificate correttamente) e con un ACK/NAK simulato via pty, ma **non**
con un modulo ZED-F9P reale — gli ID dei messaggi RTCM3/NMEA e il layout
di TMODE3 vengono dalla documentazione pubblica u-blox e vanno confermati
sul tuo hardware (vedi il disclaimer in testa a `drivers/ublox.py`). Per
aiutare la verifica, ogni comando ora attende e logga esplicitamente la
risposta UBX-ACK-ACK/NAK del modulo, invece di inviare "alla cieca" come
nella prima versione — un log `NAK` o `nessuna risposta` è già un segnale
diagnostico affidabile. Vedi **`../VERIFICA_HARDWARE.md`** per il
protocollo di test completo (driver, `str2str`, caster locale, resilienza
USB, survey-in/PPP) da seguire con hardware reale.

Entità, topic MQTT (`gnssbase/...`), device (`RTK Base Station`, con
`model` impostato a runtime sul nome del driver selezionato) e file di log
(`gnssbase_*.rtcm3`) sono ora generici, senza riferimenti a "UM982" —
coerente con il fatto di supportare più ricevitori. Essendo un progetto
non ancora installato su un'istanza reale, questo rebranding non ha dovuto
preoccuparsi di rompere `unique_id`/entità esistenti; se in futuro cambi
ancora questi nomi su un'installazione già in uso, le vecchie entità
resteranno "orfane" in HA finché non le rimuovi manualmente.

## Relazione con gli altri file della cartella `um982/`

Questo add-on è una delle tre strade percorse per la stessa base RTK,
documentate nella cartella principale:

- **`../ISTRUZIONI.md`** — procedura manuale via script Python +
  ESP32 ([esp32-ntrip-DUO](https://github.com/designer2k2/esp32-ntrip-DUO/)).
  Questo add-on **sostituisce interamente** quella procedura se usi Home
  Assistant: internalizza `configure_um982.py` (in `drivers/unicore.py`),
  `ppp_process.py` (in `ppp.py`, richiamato dalla campagna PPP) e
  `set_um982_base.py` (posizione manuale/survey-in/campagna PPP), e non
  richiede l'ESP32 perché fa già lui da server NTRIP multi-caster.
- **`../RTKBASE_PROXMOX.md`** — alternativa con RTKBase su una VM Proxmox.
  Copre lo stesso caso d'uso (multi-caster + gestione UM982) ma fuori da
  Home Assistant, senza il pannello skyplot integrato.
- Gli script standalone (`../ppp_process.py`, `../configure_um982.py`,
  `../set_um982_base.py`) restano utili come riferimento indipendente
  dall'add-on, ad esempio per rielaborare manualmente un log raw o per
  verificare/confrontare un risultato PPP fuori dal container.

Se stai partendo da zero e usi già Home Assistant Supervised/OS, questo
add-on è il percorso più diretto: non serve seguire `ISTRUZIONI.md` passo
per passo, basta questo README.

## Nota terminologica: HACS vs Add-on

Questo è un **Add-on Supervisor** (container Docker), non un'integrazione
HACS. Si installa con lo stesso meccanismo "aggiungi repository Git" che
usi con HACS, ma dal menu nativo di Home Assistant:
**Impostazioni → Add-on → Add-on Store → ⋮ (in alto a destra) → Repository**,
incollando l'URL di questo repository. Funziona solo su installazioni
**Home Assistant OS** o **Supervised** — non su "Home Assistant Container"
puro, che non ha il Supervisor.

## Prerequisiti

- Un broker MQTT raggiungibile da Home Assistant (add-on "Mosquitto broker"
  o integrazione MQTT già configurata).
- Il ricevitore GNSS (UM982, u-blox, ...) collegato via USB/seriale
  all'host che esegue Home Assistant.

## Configurazione

Nell'add-on, scheda "Configuration":

```yaml
receiver_type: unicore_um98x   # oppure: ublox_zedf9p
rtcm_port: /dev/ttyUSB0
nmea_port: /dev/ttyUSB0
baudrate: 115200
survey_in_duration_sec: 300
ntrip_casters:
  - host: rtk2go.com
    port: 2101
    mountpoint: TUOMOUNTPOINT
    password: ""
  - host: caster-privato.example.com
    port: 2101
    mountpoint: BASE1
    password: "password"
```

Nota: niente campo `user`. RTKLIB, nel ruolo server/encoder (`str2str -out
ntrips://...`), si autentica verso il caster solo con la **password del
mountpoint** (protocollo NTRIP1 `SOURCE <password> <mountpoint>`) — uno
username non avrebbe alcun effetto (verificato leggendo `reqntrip_s` in
`src/stream.c` di RTKLIB). Per RTK2go, la password del mountpoint è quella
scelta al momento della registrazione del mountpoint stesso.

Se `rtcm_port` e `nmea_port` coincidono, l'add-on invia comunque entrambi i
set di comandi di configurazione sulla stessa porta (RTCM3 + NMEA GGA/GST
convivono sullo stesso stream seriale senza problemi per `str2str`, che
estrae solo i frame RTCM ignorando il resto).

### Più caster NTRIP contemporaneamente

`ntrip_casters` è una lista: dalla UI dell'add-on (scheda Configuration)
puoi aggiungere o rimuovere voci con i pulsanti +/-, una per ogni caster a
cui vuoi inviare le correzioni (es. RTK2go + un caster privato + un altro
ancora). Le voci con `host` vuoto vengono ignorate. Tecnicamente tutte
condividono una singola istanza `str2str` che legge la seriale una volta
sola e la smista su più `-out` (uno per caster, più uno per il log raw
continuo usato dalla campagna PPP) — leggere la stessa porta seriale da
processi `str2str` separati corromperebbe lo stream, per questo è
importante che sia sempre un solo processo con più `-out` e non un
processo per caster.

**Limite non ovvio, verificato nel codice sorgente di RTKLIB**: `str2str`
supporta al massimo **4 output totali** (`MAXSTR=5` in `str2str.c`: 1
input + 4 output — un quinto `-out` viene ignorato in modo silenzioso e
imprevedibile, non con un errore). Uno di questi slot è sempre occupato
dal log raw continuo, e uno dal caster locale se `caster_enabled: true`.
Quindi il numero massimo di caster esterni configurabili è:
- **3** se `caster_enabled: false`;
- **2** se `caster_enabled: true`.

Se superi questo limite, l'add-on **non avvia `str2str`** (per evitare il
comportamento indefinito di RTKLIB) e pubblica il motivo in
`sensor.errore_di_configurazione` — il resto dell'add-on (skyplot, survey-in,
ecc.) continua a funzionare normalmente nel frattempo.

### Stato reale delle connessioni ai caster (non solo "il processo è vivo")

In precedenza l'unico controllo era che il processo `str2str` fosse in
esecuzione — se un caster rifiutava l'autenticazione, `str2str` restava
comunque "vivo" senza che nulla lo segnalasse. Ora l'add-on legge lo
stderr di `str2str`, che stampa periodicamente (ogni 5s) una riga di
stato reale per ciascun output (formato verificato compilando e lanciando
davvero il binario, non dedotto), e la traduce in `sensor.stato_uscita_N`
(uno per ogni caster/log/relay locale, nello stesso ordine in cui sono
configurati) + `sensor.diagnostica_str2str` con il testo esatto dell'errore
quando presente (es. una password sbagliata mostra l'errore del caster,
non un generico "errore").

### Fare da caster per i rover (senza caster esterno)

Di default l'add-on fa solo da **client/uploader** verso i caster esterni
elencati in `ntrip_casters` (come i servizi `str2str_ntrip_A/B` di
RTKBase) — non risponde a connessioni in ingresso.

Impostando `caster_enabled: true`, l'add-on diventa anche un **NTRIP
Caster** minimale (handshake NTRIP v1/ICY, con sourcetable e Basic Auth
opzionale) sulla porta **2101/tcp**, esposta dalla scheda "Network"
dell'add-on (rimappabile su una porta host diversa da lì). I rover si
collegano con:

```yaml
caster_mountpoint: "GNSSBASE"   # path a cui i rover si connettono: /GNSSBASE
caster_user: ""              # vuoto = nessuna autenticazione richiesta
caster_password: ""
caster_max_clients: 10       # rover contemporanei accettati, oltre rifiutati con 503
```

Funziona in parallelo ai caster esterni: tutti condividono la stessa
istanza `str2str`, che oltre agli `-out ntrips://...` e al log su file
riceve anche un `-out tcpcli://127.0.0.1:28101` verso un piccolo relay
interno (`caster.py`), che ridistribuisce i byte RTCM a tutti i rover
connessi. Il numero di rover connessi è esposto come
`sensor.rover_connessi_caster_locale`.

**Hardening incluso**: dopo troppe password sbagliate dallo stesso IP (5
tentativi in 60s di default) l'IP viene bloccato per 5 minuti, anche se poi
invia la password corretta — rende poco pratico un bruteforce della
password del mountpoint. Il numero massimo di rover connessi
contemporaneamente è configurabile con `caster_max_clients` (default 10):
oltre il limite, nuove connessioni vengono rifiutate con `503`.

Limiti noti di questa implementazione minimale (adeguata per un uso
personale/di piccola scala, non per un caster pubblico ad alto traffico):
- gestisce un solo mountpoint (quello configurato), non un sourcetable con
  più stazioni;
- il blocco anti-bruteforce è per IP e in memoria (si azzera se l'add-on
  riparte) — non protegge da un attacco distribuito su molti IP, e può
  bloccare erroneamente più utenti legittimi dietro lo stesso NAT/IP
  pubblico se sbagliano la password troppe volte;
- nessuna cifratura TLS (NTRIP in chiaro, come la maggior parte dei caster
  "semplici" — usa una VPN o un tunnel se serve esporlo oltre la LAN; non
  implementato in questa passata, possibile follow-up separato).

## Cosa succede se l'USB non è collegata (o si scollega)

L'add-on è resiliente a USB assente/scollegata, sia all'avvio che durante
il funzionamento:

- **All'avvio**, se `rtcm_port`/`nmea_port` non esistono ancora (es. l'USB
  non è ancora enumerata quando l'add-on parte), l'add-on **aspetta**
  invece di terminare, ritentando ogni 5 secondi finché la porta non
  compare.
- **A runtime**, il thread che legge l'NMEA rileva sia la scomparsa della
  porta (device rimosso) sia un silenzio prolungato (>15s senza alcun
  dato, anche se la porta esiste ancora): in entrambi i casi richiude la
  connessione e ritenta periodicamente, senza mai far terminare il
  processo dell'add-on.
- Lo stato è visibile in Home Assistant tramite
  `binary_sensor.dispositivo_connesso` (ON/OFF) e
  `sensor.ultimo_dato_ricevuto` (timestamp dell'ultimo dato NMEA
  valido) — puoi costruirci sopra un'automazione (es. notifica se resta
  OFF per più di N minuti).
- `str2str` ha già un watchdog separato (`watchdog_str2str`) che lo
  riavvia se termina inaspettatamente, indipendentemente da questo
  meccanismo.

Nota: non essendoci hardware reale a disposizione in fase di sviluppo,
questa resilienza è stata verificata simulando l'USB con una pty
(pseudo-terminale) — assenza iniziale, comparsa a runtime, e
disconnessione durante il funzionamento — non con un vero adattatore
USB-seriale. Il comportamento con un vero cavo USB scollegato dovrebbe
essere equivalente (il kernel rimuove il device node `/dev/ttyUSB0` o
`/dev/ttyACM0`), ma vale la pena confermarlo al primo test reale.

## Survey-In vs Campagna PPP: leggi questo prima di usarlo

Ci sono ora **due modi** per fissare la posizione della base, con
precisione molto diversa:

- **Survey-In rapido** (`button.avvia_survey_in`): media di
  posizioni standalone (single-point, non differenziali) raccolte per
  `survey_in_duration_sec` secondi. Accuratezza tipica
  **metrica/sub-metrica**. Utile per test rapidi o installazioni non
  critiche, non per un riferimento RTK di produzione.

- **Campagna PPP** (`button.avvia_campagna_ppp`): l'add-on registra
  già in continuo il flusso RTCM raw su `/data/raw_logs` (con rotazione
  oraria, retention configurabile via `raw_log_retention_hours`). Avviando
  la campagna, l'add-on aspetta `ppp_duration_hours` ore, poi elabora
  automaticamente i file accumulati in quella finestra con lo stesso
  procedimento di `ppp_process.py` (convbin → download prodotti IGS →
  rnx2rtkp PPP-static) e applica il risultato come base fissa. Accuratezza
  attesa **centimetrica** con log di diverse ore (più a lungo registri,
  meglio converge, specialmente in quota). **Questo è il metodo da usare
  per l'installazione definitiva**, non il survey-in rapido.

Il risultato della campagna PPP viene anche pubblicato nei campi
"posizione manuale", cosicché resti visibile/riapplicabile anche in
seguito tramite `button.applica_posizione_manuale`.

### Tempo rimanente e annullamento

Entrambe le procedure espongono un conto alla rovescia
(`sensor.survey_in_tempo_rimanente` / `sensor.campagna_ppp_tempo_rimanente`,
in secondi, aggiornato ogni secondo) e possono essere interrotte con
`button.annulla_survey_in` / `button.annulla_campagna_ppp`: lo stato passa
a `cancelled` e **nessuna posizione viene applicata al ricevitore**.

Limite noto: per la campagna PPP l'annullamento funziona solo durante la
fase `logging` (l'attesa che accumula il log raw). Una volta passata alla
fase `processing` (conversione RINEX + download prodotti IGS + PPP), la
campagna va a termine — interromperla a metà rischierebbe di lasciare file
temporanei incoerenti a metà elaborazione. In pratica non è un limite
stringente: la fase `processing` dura tipicamente pochi minuti contro le
ore della fase `logging`.

### Backup della posizione calcolata

Ogni volta che una posizione viene fissata (survey-in, campagna PPP, o
applicazione manuale), l'add-on salva su `/data/position_backup.json` un
piccolo JSON con **la provenienza**, non solo il valore: lat/lon/height,
metodo (`survey_in`/`ppp`/`manual`), data/ora, driver del ricevitore usato,
e parametri specifici del metodo (es. numero di campioni per il survey-in,
ore/file per la campagna PPP). Lo stesso contenuto è esposto come
`sensor.ultima_posizione_calcolata` (stato = timestamp, attributi = resto
dei dati).

Perché: il ricevitore potrebbe salvare la posizione nella propria
configurazione interna (se `saveconfig` va a buon fine), ma se il
container/add-on viene ricreato da zero, senza questo backup l'add-on non
ricorderebbe più *come* o *quando* è stata calcolata quella posizione — i
campi "posizione manuale" tornerebbero vuoti finché non li reimposti a
mano. All'avvio, se il backup esiste, viene ripristinato automaticamente
nei campi "posizione manuale" (solo in memoria/MQTT: **non** viene
rimandato al ricevitore in automatico — resta un'azione deliberata tramite
`button.applica_posizione_manuale`, se decidi di riapplicarlo).

Nota: la campagna PPP richiede accesso internet in uscita dal container
per scaricare i prodotti IGS (SP3/CLK/ANTEX) da `files.igs.org` (pubblico,
nessuna credenziale richiesta).

## Test automatizzati (eseguibili fuori da Home Assistant)

Tutta la logica non legata a un vero Supervisor/broker/hardware ha una
suite `pytest` in `gnss_rtk_base/tests/`, pensata per girare in locale o in
CI senza bisogno di Home Assistant, Docker, RTKLIB o un ricevitore fisico:

```bash
cd gnss_rtk_base
pip install -r requirements-dev.txt
pytest -v
```

Cosa copre (77 test): parsing NMEA (GGA/GST/GSV/GSA), driver Unicore e
u-blox (compreso il parsing UBX-ACK/NAK simulando un modulo che risponde
ACK/NAK), il registro driver, il caster NTRIP locale (sourcetable, auth,
relay dei byte), la logica PPP che non richiede binari esterni (selezione
file, parsing date/posizione, download con mirror mockati), le entità MQTT
Discovery, la resilienza USB (`main.py`) simulata con una pty, il limite
dei 4 output di `str2str` e la correzione del path `/dev/` — incluso un
test di regressione per il bug del loop `for caster in ntrip_casters` che
oscurava il modulo `caster` (vedi changelog di questa conversazione).

Un test (`test_monitor_str2str_status_against_real_str2str_binary`) è
un'**integrazione reale** con il binario `str2str` vero e proprio (non
simulato): gira solo se `str2str` è nel `PATH` (`pip install -r
requirements-dev.txt` non lo installa — va compilato da RTKLIB, vedi
`Dockerfile`), altrimenti viene automaticamente saltato (`SKIPPED`), così
la suite resta eseguibile ovunque senza richiedere RTKLIB come
prerequisito rigido.

Cosa **non** è coperto da questa suite (richiede hardware reale) — vedi
invece **`../VERIFICA_HARDWARE.md`**:
- se i comandi Unicore/u-blox vengono davvero applicati da un modulo reale;
- se `convbin`/`rnx2rtkp` (RTKLIB) si comportano come previsto sui dati di
  un ricevitore vero (str2str invece è ora testato anche con un binario
  reale, vedi sopra);
- se un vero client NTRIP da campo (SW Maps, Emlid Flow, ecc.) si collega
  correttamente al caster locale, o un vero caster esterno (RTK2go) accetta
  le correzioni.

## Cose da verificare/adattare prima dell'uso reale

- **Sintassi comandi del driver selezionato** (`drivers/unicore.py` o
  `drivers/ublox.py`): per Unicore, `mode base`, `mode rover`,
  `log ... ontime ...` sono la sintassi più probabile per l'UM982; per
  u-blox, ID messaggi RTCM3/NMEA e layout TMODE3 vengono dalla
  documentazione pubblica. In entrambi i casi vanno confermati sul
  command/interface manual del tuo modulo: controlla i log dell'add-on per
  eventuali comandi non riconosciuti o non applicati.
- ~~Path di build di RTKLIB~~ — **risolto**: pinnato al tag `v2.4.3-b34`
  e i path corretti (`app/consapp/<tool>/gcc`) sono stati verificati
  clonando quel tag esatto e compilando davvero `str2str` (non solo
  ipotizzati). Resta da verificare solo il comportamento su Alpine
  (il build reale di test è stato fatto su Debian/glibc).
- ~~Sintassi `str2str` con più `-out`~~ — **risolto e verificato con un
  binario reale compilato dal tag pinnato**, non solo letto dall'help
  text: la sintassi `-in stream [-out stream [-out stream...]]`, i formati
  `serial://`, `file://`, `ntrips://`, `tcpcli://`, e la riga di stato su
  stderr (usata ora per `sensor.stato_uscita_N`) sono stati osservati
  davvero lanciando il binario con una pty come input. Trovati così due
  bug reali, entrambi corretti: (1) `serial://` non vuole il prefisso
  `/dev/` (str2str lo prepone da solo — passare il path completo lo
  raddoppiava, e `str2str` non partiva mai, **su nessuna configurazione
  precedente di questo add-on**); (2) `str2str` accetta **al massimo 4
  `-out` totali** (`MAXSTR=5` nel sorgente), limite ora imposto
  esplicitamente (vedi sopra). Non ancora verificati: comportamento su
  Alpine (test fatto su Debian/glibc) e un caster reale in produzione
  (RTK2go o simile) — solo un caster locale/irraggiungibile simulato.
  Non testata la rotazione oraria del file di log (`%Y%m%d%h`), che non è
  stata necessaria attivare durante questa verifica.
- **RTKLIB implementa già un ruolo di caster nativo**, scoperto leggendo
  il sorgente durante questa verifica: `str2str -out ntripc://[user:passwd@][:port]/mntpnt[:srctbl]`
  fa esattamente quello che fa `caster.py` in questo add-on (accetta
  connessioni multiple, HTTP/1.1, sourcetable, autenticazione), a
  differenza di quanto affermato in una fase precedente di questo
  progetto (si era concluso — erroneamente — che RTKLIB non supportasse
  affatto il ruolo di caster). Non è stato ancora valutato se sostituire
  `caster.py` con questa funzionalità nativa: il vantaggio sarebbe meno
  codice custom da mantenere, lo svantaggio è perdere il conteggio dei
  rover connessi che oggi `caster.py` espone via MQTT (`str2str` non pare
  offrire un modo diretto di riportare quel dato all'esterno).
- **Spazio disco**: il log raw continuo in `/data/raw_logs` cresce con il
  tempo fino alla retention configurata (`raw_log_retention_hours`, default
  72h). Dimensiona la retention in base allo spazio disponibile sull'host.
- **Skyplot, satelliti "usati"**: il flag `used` confronta i PRN letti da
  GSA con quelli visti in GSV senza distinguere la costellazione (la
  numerazione NMEA può sovrapporsi tra costellazioni in ricevitori
  multi-GNSS). Per una visualizzazione indicativa va bene; se noti
  incongruenze evidenti, verifica come l'UM982 numera i satelliti nei GSA
  multi-costellazione e adatta `nmea.parse_gsa`/`state.py` di conseguenza.
- **Caster locale**: la logica di handshake/sourcetable/auth/relay è stata
  testata con un client socket grezzo (sourcetable, 401, ICY 200 OK, e
  inoltro dei byte funzionano), ma non ancora con un vero client NTRIP da
  campo (es. SW Maps, Emlid Flow, u-center): se un'app rover non si
  connette, verifica con Wireshark/tcpdump cosa si aspetta esattamente nel
  primo scambio (alcune app inviano `Ntrip-Version: Ntrip/2.0` o intestazioni
  aggiuntive che questa implementazione minimale ignora).

## File

- `repository.yaml` — manifest del repository per lo store add-on.
- `gnss_rtk_base/config.yaml` — manifest dell'add-on (opzioni, architetture).
- `gnss_rtk_base/build.yaml` — immagini base per arch.
- `gnss_rtk_base/Dockerfile` — build immagine (RTKLIB da sorgente + Python).
- `gnss_rtk_base/run.sh` — entrypoint (bashio, credenziali MQTT da Supervisor).
- `gnss_rtk_base/main.py` — logica applicativa (str2str, MQTT, survey-in, campagna PPP).
- `gnss_rtk_base/drivers/` — driver per ricevitore (`base.py` contratto,
  `unicore.py`, `ublox.py`, `__init__.py` registro); vedi "Supporto
  multi-ricevitore" sopra per come aggiungerne uno nuovo.
- `gnss_rtk_base/nmea.py` — parsing GGA/GST/GSV/GSA (standard NMEA-0183, non specifico di un ricevitore).
- `gnss_rtk_base/ppp.py` — elaborazione PPP-static (porting di `ppp_process.py`).
- `gnss_rtk_base/state.py` — stato condiviso in memoria (satelliti/fix) tra il monitor NMEA e il web server.
- `gnss_rtk_base/webui.py` — server HTTP minimale (stdlib) per il pannello skyplot via Ingress.
- `gnss_rtk_base/www/index.html` — pagina skyplot (canvas, nessuna dipendenza esterna).
- `gnss_rtk_base/mqtt_discovery.py` — helper per le entità MQTT Discovery.
- `gnss_rtk_base/caster.py` — mini NTRIP Caster locale opzionale (`caster_enabled`).
- `gnss_rtk_base/position_backup.py` — backup su disco della posizione calcolata, con provenienza.
- `gnss_rtk_base/requirements-dev.txt` — dipendenze extra per i test (`pytest`).
- `gnss_rtk_base/pytest.ini` — configurazione pytest (`testpaths = tests`).
- `gnss_rtk_base/tests/` — suite di test automatizzati, eseguibile fuori
  da Home Assistant (vedi sezione "Test automatizzati" sopra).
