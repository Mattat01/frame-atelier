ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8

# Pillow comes from Alpine's package repo to avoid a slow source build.
RUN apk add --no-cache python3 py3-pip py3-pillow

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app.py /app/app.py
COPY www /app/www
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
