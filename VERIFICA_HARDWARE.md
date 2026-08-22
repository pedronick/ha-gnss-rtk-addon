# Protocollo di verifica su hardware reale

Checklist da seguire la prima volta che colleghi un ricevitore fisico
(Unicore UM982 o u-blox ZED-F9P/M8P) a questo add-on. Segna cosa ha
funzionato e cosa no: gran parte del codice protocollo-specifico
(`drivers/`, `caster.py`, la rotazione dei file di `str2str`) è stato
verificato solo con simulazioni software, mai con un modulo reale — vedi
"Cose da verificare" nel `README.md` dell'add-on.

## 1. Driver del ricevitore

Avvia l'add-on con `receiver_type` impostato sul tuo modulo e guarda i log
(scheda "Log" dell'add-on).

**Se usi Unicore (`unicore_um98x`)**: ogni comando stampa `>>` seguito
dalla risposta grezza del modulo (`<<`). Verifica a occhio che non ci
siano errori (il formato esatto della risposta OK/errore dipende dal
firmware — non viene interpretato automaticamente).

**Se usi u-blox (`ublox_zedf9p`)**: da questa versione ogni comando logga
esplicitamente `-> ACK`, `-> NAK (comando rifiutato dal modulo!)`, o
`-> nessuna risposta entro il timeout`. Un log diverso da `ACK` è un
segnale affidabile di problema:
- **NAK**: il modulo ha capito il comando ma lo ha rifiutato — controlla
  che l'ID del messaggio RTCM/NMEA sia corretto per il tuo firmware
  (`drivers/ublox.py`, dizionari `RTCM_MSG_IDS`/`NMEA_MSG_IDS`).
- **Nessuna risposta**: probabile problema di porta/baud rate, non di
  contenuto del comando (il modulo non sta nemmeno ricevendo il frame).

Cosa verificare concretamente:
- [ ] Tutti i comandi di `configure_rtcm`/`configure_nmea` ricevono ACK
      (u-blox) o risposta senza errori (Unicore).
- [ ] Dopo la configurazione, il modulo emette effettivamente NMEA GGA a
      1Hz sulla porta indicata (verificabile aprendo la porta con un
      terminale seriale, es. `screen /dev/ttyUSB0 115200`).
- [ ] Il modulo emette RTCM3 (dati binari non leggibili) sulla porta RTCM.

## 2. `str2str` (RTKLIB)

- [ ] Nei log dell'add-on, il comando `str2str` stampato in avvio si
      avvia senza errori immediati.
- [ ] Se hai configurato un caster esterno (`ntrip_casters`), verifica
      lato caster (es. dashboard di RTK2go) che il mountpoint risulti
      online e riceva dati.
- [ ] Verifica che compaiano file in `/data/raw_logs/gnssbase_*.rtcm3`
      (accessibile via terminale SSH nell'host, o addon "File editor"/
      "Studio Code Server" di Home Assistant) e che un **nuovo file
      compaia ogni ora** (verifica la rotazione basata su `%Y%m%d%h`,
      mai testata con un vero build di RTKLIB).
- [ ] Se un file cresce ma resta a 0 byte per minuti, il modulo
      probabilmente non sta fornendo RTCM valido sulla porta configurata.

## 3. Caster locale (se `caster_enabled: true`)

- [ ] Da un secondo dispositivo sulla stessa rete, prova a collegarti con
      un **vero client NTRIP** (non solo un test a mano):
      - App mobile: SW Maps, Emlid Flow, o simili (modalità "NTRIP
        client" personalizzata, con host/porta/mountpoint/credenziali
        dell'add-on).
      - Da PC: `str2str -in ntrip://user:pass@<ip-addon>:2101/<mountpoint> -out file://test.rtcm3`
        e verifica che `test.rtcm3` cresca nel tempo.
- [ ] Controlla `sensor.rover_connessi_caster_locale` in Home Assistant:
      deve incrementare quando un client si connette.
- [ ] Se il client si connette ma non riceve dati, il problema è quasi
      certamente nell'handshake (vedi nota "Ntrip-Version: Ntrip/2.0" nel
      README) — cattura il traffico con `tcpdump -i any port 2101 -w cattura.pcap`
      e ispeziona con Wireshark cosa manda il client prima di ricevere
      risposta.

## 4. Resilienza USB

- [ ] Scollega fisicamente il cavo USB del ricevitore mentre l'add-on
      gira: `binary_sensor.dispositivo_connesso` deve passare a OFF entro
      ~15-20 secondi.
- [ ] Ricollega il cavo: deve tornare ON entro pochi secondi, senza dover
      riavviare l'add-on.
- [ ] Riavvia l'host/la VM con il ricevitore già collegato: l'add-on deve
      partire correttamente anche se l'USB non è ancora enumerata nei
      primi istanti del boot.

## 5. Survey-in e campagna PPP

- [ ] `button.avvia_survey_in`: dopo `survey_in_duration_sec`, il modulo
      deve risultare in modalità base fissa con coordinate plausibili
      (confrontabili a occhio con la posizione reale, es. da Google Maps,
      tolleranza attesa: qualche metro).
- [ ] `button.avvia_campagna_ppp`: a campagna conclusa, confronta la
      posizione ottenuta con quella calcolata indipendentemente da
      `ppp_process.py` (cartella principale) o dal servizio online
      CSRS-PPP sullo stesso intervallo di log — devono coincidere entro
      pochi centimetri.

## Cosa fare se qualcosa non torna

Segnami esattamente: quale punto della checklist fallisce, il
`receiver_type` in uso, e l'output rilevante dei log dell'add-on (in
particolare le righe `[unicore]`/`[ublox]`/`[main]`/`[caster]`). Da lì
posso capire se il problema è nella sintassi dei comandi, nel parsing, o
altrove.
