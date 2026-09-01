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

# patches/0001-rtklib-gentcp-getaddrinfo.patch: RTKLIB's gentcp() (used by
# every TCP client stream - ntrips://, tcpcli://) calls the legacy
# gethostbyname() to resolve the target address, even a plain numeric IP.
# On this musl/Alpine base image that corrupts memory and crashes str2str
# with a real SIGSEGV as soon as any TCP client output receives data -
# reproduced with a real build of this exact image and a debugger
# backtrace (gentcp/waittcpcli/writetcpcli in stream.c), on a user's real
# Home Assistant instance and confirmed here. This means every external
# NTRIP caster output (ntrip_casters) was silently broken on Alpine/musl.
# The patch replaces it with getaddrinfo(), the modern, thread-safe,
# POSIX-standard replacement - unaffected on both glibc and musl, verified
# by rebuilding this exact image and reproducing the crash's disappearance.
COPY patches /tmp/patches

# rnx2rtkp also links against lib/iers (Fortran tidal corrections): it
# must be built first, otherwise linking failed with "cannot find
# .../lib/iers/gcc/iers.a" and "cannot find -lgfortran" (error verified
# for real in a build, not just theoretical: gfortran was missing and
# iers.a was never built).
RUN git clone --depth 1 --branch "$RTKLIB_REF" https://github.com/tomojitakasu/RTKLIB.git /tmp/RTKLIB \
    && (cd /tmp/RTKLIB && git apply /tmp/patches/0001-rtklib-gentcp-getaddrinfo.patch) \
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
