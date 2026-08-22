"""
Contratto che ogni driver ricevitore deve implementare.

Non è una classe abstract-base per scelta deliberata: i driver di questo
progetto sono moduli con funzioni allo stesso nome, non istanze — più
semplice da leggere/estendere per un progetto di questa dimensione. Questo
file documenta solo la firma richiesta; ogni driver in questo package deve
esporre esattamente queste quattro funzioni:

    configure_rtcm(port: str, baud: int) -> None
        Abilita sulla porta indicata l'output RTCM3 minimo richiesto da
        questo add-on: 1005 (coordinate stazione) + MSM (o equivalente)
        per GPS/GLONASS/Galileo/BeiDou.

    configure_nmea(port: str, baud: int) -> None
        Abilita sulla porta indicata GGA, GST, GSV, GSA a ~1Hz (usati da
        nmea.py per fix/satelliti/accuratezza/skyplot). Se il ricevitore
        non supporta uno di questi messaggi (es. niente GST), va bene
        ometterlo: main.py gestisce già l'assenza dei dati corrispondenti.

    set_rover_mode(port: str, baud: int) -> None
        Mette il ricevitore in modalità standalone (nessuna posizione
        base fissata), usata prima del survey-in per ottenere fix non
        vincolati a una posizione precedente.

    set_fixed_base(port: str, baud: int, lat: float, lon: float, height: float) -> None
        Imposta il ricevitore in modalità base con posizione fissa
        (gradi decimali WGS84, altezza ellissoidica in metri) e salva la
        configurazione se il ricevitore lo richiede esplicitamente.

Un nuovo driver per un altro ricevitore va aggiunto come nuovo modulo in
questo package (es. drivers/septentrio.py) con queste stesse quattro
funzioni, poi registrato in drivers/__init__.py.
"""
