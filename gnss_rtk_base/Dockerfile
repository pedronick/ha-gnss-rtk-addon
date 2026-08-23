ARG BUILD_FROM
FROM $BUILD_FROM

# Dipendenze di build e runtime.
#
# RTKLIB è pinnato al tag v2.4.3-b34 (commit 180043ee24b6d2b168f98b64be15f69d50046b1a),
# l'ultima release stabile al momento della scrittura, per build riproducibili
# (in precedenza si clonava sempre l'HEAD del branch, senza alcuna garanzia di
# stabilità nel tempo). Path e comando di build VERIFICATI compilando
# effettivamente str2str/convbin/rnx2rtkp da questo tag esatto: la struttura
# reale è "app/consapp/<tool>/gcc" (non "app/<tool>/gcc" come ipotizzato in una
# versione precedente di questo Dockerfile, che non avrebbe mai funzionato).
ARG RTKLIB_REF=v2.4.3-b34

RUN apk add --no-cache \
        python3 \
        py3-pip \
        git \
        build-base \
        gfortran \
        linux-headers

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# rnx2rtkp si linka anche contro lib/iers (correzioni di marea, in Fortran):
# va compilata per prima, altrimenti il link falliva con "cannot find
# .../lib/iers/gcc/iers.a" e "cannot find -lgfortran" (errore verificato
# realmente in build, non solo teorico: gfortran mancava e iers.a non
# veniva mai costruita).
RUN git clone --depth 1 --branch "$RTKLIB_REF" https://github.com/tomojitakasu/RTKLIB.git /tmp/RTKLIB \
    && (cd /tmp/RTKLIB/lib/iers/gcc && make) \
    && for app in str2str convbin rnx2rtkp; do \
        (cd /tmp/RTKLIB/app/consapp/$app/gcc && make && make install); \
       done \
    && rm -rf /tmp/RTKLIB

WORKDIR /app
COPY main.py nmea.py mqtt_discovery.py ppp.py state.py webui.py caster.py position_backup.py /app/
COPY drivers /app/drivers
COPY www /app/www
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
