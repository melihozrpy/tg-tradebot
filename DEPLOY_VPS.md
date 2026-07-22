# MERGEN QUANT VPS Deploy

Bu rehber projeyi GitHub'a yukleyip VPS uzerinde Docker ile 7/24 calistirmak icindir.

## 1. GitHub'a Yukleme

Bu dosyalar GitHub'a gitmemeli: `.env`, `.venv`, `*.db`, cache dosyalari, zip arsivleri.
`.gitignore` ve `.dockerignore` bunu engeller.

```bash
git init
git add .
git commit -m "Initial MERGEN QUANT bot"
git branch -M main
git remote add origin https://github.com/KULLANICI_ADIN/REPO_ADIN.git
git push -u origin main
```

## 2. VPS Hazirligi

Ubuntu VPS icin:

```bash
sudo apt update
sudo apt install -y git ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc > /dev/null
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

## 3. Projeyi VPS'e Alma

```bash
git clone https://github.com/KULLANICI_ADIN/REPO_ADIN.git
cd REPO_ADIN
cp .env.example .env
nano .env
```

`.env` icinde en az sunlari doldur:

```env
TELEGRAM_BOT_TOKEN=BotFather_tokenin
ADMIN_TELEGRAM_USER_IDS=telegram_user_id
TELEGRAM_MODE=polling
MARKET_DATA_PROVIDER=yfinance
GROQ_ENABLED=false
CLOSE_SCAN_ENABLED=false
```

Not: Docker Compose, veritabanini kalici volume icin otomatik olarak
`sqlite:////app/data/mergen_quant.db` ile calistirir.

## 4. Calistirma

```bash
docker compose up -d --build
docker compose logs -f bot
```

API kontrolu:

```bash
docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
```

## 5. Guncelleme

```bash
git pull
docker compose up -d --build
docker compose logs -f bot
```

## 6. Durdurma

```bash
docker compose down
```

Veritabani volume'u korunur. Tum veriyi silmek icin ayrica `docker compose down -v`
kullanilir; bunu normalde yapma.
