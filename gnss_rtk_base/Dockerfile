ARG BUILD_FROM
FROM $BUILD_FROM

# Build and runtime dependencies.
#
# RTKLIB is pinned to tag v2.4.3-b34 (commit 180043ee24b6d2b168f98b64be15f69d50046b1a),
# the latest stable release at the time of writing, for reproducible builds
# (previously the branch HEAD was always cloned, with no guarantee of
# stability over time). Path and build command VERIFIED by actually
# compiling str2str/convbin/rnx2rtkp from this exact tag: the real
# structure is "app/consapp/<tool>/gcc" (not "app/<tool>/gcc" as assumed in
# an earlier version of this Dockerfile, which would never have worked).
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

# rnx2rtkp also links against lib/iers (Fortran tidal corrections): it
# must be built first, otherwise linking failed with "cannot find
# .../lib/iers/gcc/iers.a" and "cannot find -lgfortran" (error verified
# for real in a build, not just theoretical: gfortran was missing and
# iers.a was never built).
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
