FROM python:3.11-slim

WORKDIR /app

# lxml derlemesi için gerekli sistem paketleri
RUN apt-get update && apt-get install -y \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Bağımlılıkları yükle
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodunu kopyala
COPY . .

# Portu 8000 olarak ayarla (Render bunu otomatik PORT ile override eder)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]