FROM python:3.12-slim
WORKDIR /app
# fonts-dejavu-core: python:3.12-slim liefert keine Fonts mit. Ohne dieses
# Paket faellt render_lookup_image() (bot.py) auf Pillows eingebauten
# Bitmap-Default-Font zurueck (funktioniert, sieht aber deutlich
# schlechter aus) -- siehe except-Zweig dort.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
RUN mkdir -p /app/data
CMD ["python", "-u", "bot.py"]