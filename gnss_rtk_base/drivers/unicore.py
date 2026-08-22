"""
Driver Unicore, per la famiglia UM980/UM982 (stesso set di comandi ASCII).

ATTENZIONE: la sintassi esatta dei comandi Unicore può variare tra
firmware/revisioni. Verifica sempre nel log dell'add-on (stdout) che ogni
comando riceva una risposta di conferma dal modulo; se un comando non è
riconosciuto, controlla il command manual UM982 e adatta le stringhe qui
sotto (alcune revisioni usano "rtcm1077 1" diretto invece di
"log rtcm1077 ontime 1"). Contratto del driver: vedi drivers/base.py.
"""

import time

import serial

NAME = "Unicore UM980/UM982"


def send_commands(port, baud, commands):
    with serial.Serial(port, baud, timeout=1) as ser:
        time.sleep(0.3)
        ser.reset_input_buffer()
        for cmd in commands:
            print(f"[unicore] >> {cmd}", flush=True)
            ser.write((cmd + "\r\n").encode("ascii"))
            time.sleep(0.2)
            resp = ser.read(ser.in_waiting or 1)
            if resp:
                print("[unicore] <<", resp.decode("ascii", errors="replace").strip(), flush=True)


def configure_rtcm(port, baud):
    """Abilita RTCM3 (coordinate stazione + osservazioni MSM7) sulla porta indicata."""
    send_commands(port, baud, [
        "log rtcm1005 ontime 10",
        "log rtcm1077 ontime 1",
        "log rtcm1087 ontime 1",
        "log rtcm1097 ontime 1",
        "log rtcm1127 ontime 1",
        "saveconfig",
    ])


def configure_nmea(port, baud):
    """Abilita GGA (fix/satelliti), GST (accuratezza), GSV (satelliti in
    vista, per lo skyplot) e GSA (satelliti usati nel fix + DOP) sulla
    porta indicata.

    Se rtcm_port == nmea_port, questa funzione va chiamata dopo
    configure_rtcm() sulla stessa porta: i due set di messaggi si sommano.
    """
    send_commands(port, baud, [
        "log gga ontime 1",
        "log gst ontime 1",
        "log gsv ontime 1",
        "log gsa ontime 1",
        "saveconfig",
    ])


def set_rover_mode(port, baud):
    """Mette il ricevitore in modalità rover (posizionamento standalone),
    necessario prima di un survey-in per ottenere fix non vincolati a una
    posizione base già fissata."""
    send_commands(port, baud, ["mode rover"])


def set_fixed_base(port, baud, lat, lon, height):
    """Imposta il modulo in modalità BASE con posizione fissa (gradi
    decimali WGS84, altezza ellissoidica in metri) e salva la configurazione."""
    send_commands(port, baud, [
        f"mode base {lat:.8f} {lon:.8f} {height:.3f}",
        "saveconfig",
    ])
