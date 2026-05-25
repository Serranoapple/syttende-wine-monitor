Syttende Vinkort Monitor
Automatiseret system der dagligt overvager Restaurant Syttendes vinkort,
registrerer aendringer og viser EU-lavpriser fra Wine-Searcher.
100% gratis -- ingen betalte API'er eller licenser.
---
Gratis teknologi-stack
Komponent	Teknologi	Pris
Database	Supabase (gratis tier)	0 kr/md
Frontend	Vercel (gratis tier)	0 kr/md
Cron / scraper	GitHub Actions	0 kr/md
PDF-parsing	pdfplumber + regex	0 kr/md
Wine-Searcher	Web scraping	0 kr/md
---
Opsaetning
1. Supabase
Opret gratis konto pa supabase.com
Nyt projekt -> SQL Editor -> indsaet og kør supabase/schema.sql
Gem fra Settings -> API:
Project URL
anon key (til frontend)
service_role key (til scraper)
2. GitHub
Opret nyt repository og push alle filer
Settings -> Secrets and variables -> Actions:
Secret	Hvad
SUPABASE_URL	Din Supabase Project URL
SUPABASE_SERVICE_KEY	Service role key
Kør manuelt første gang:
Actions -> Syttende Vinkort Scraper -> Run workflow
3. Vercel (anbefalet -- bedre gratis tier end Render)
Gå til vercel.com og opret konto med GitHub
"Add New Project" -> import dit repository
Framework Preset: Vite
Root Directory: frontend
Environment Variables:
VITE_SUPABASE_URL
VITE_SUPABASE_ANON_KEY
Deploy
Alternativt kan render.yaml bruges pa render.com (Static Site).
---
Filstruktur
    syttende-wine-monitor/
    |-- .github/
    |   +-- workflows/
    |       +-- daily-scrape.yml
    |-- frontend/
    |   |-- src/
    |   |   |-- App.jsx
    |   |   +-- main.jsx
    |   |-- index.html
    |   |-- package.json
    |   +-- vite.config.js
    |-- scraper/
    |   |-- scraper.py
    |   +-- requirements.txt
    |-- supabase/
    |   +-- schema.sql
    |-- render.yaml
    +-- README.md

---
Hvad scraperen registrerer
Tilfojet -- ny vin (inkl. Wine-Searcher EU-lavpris link)
Fjernet -- vin udgaet fra kortet
Prisaendring -- glas- eller flaskepris aendret
Scraperen bruger SHA256-hashing sa den kun arbejder
nar en PDF rent faktisk er aendret.
