FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MIBDIRS=/usr/share/snmp/mibs:/app/mibs

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      nmap \
      iputils-ping \
      iproute2 \
      net-tools \
      snmp \
      git \
      curl \
      ca-certificates \
      gcc \
      libpq-dev && \
    mkdir -p /app/mibs /usr/share/snmp/mibs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY mibs /app/mibs
COPY app /app/app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
