# varfinnsdet.se

Realtidsbevakning av läkemedelslager på Sveriges apotek. Söker du ett läkemedel som är restnoterat kan du sätta upp en bevakning — du får e-post så fort det finns i lager igen.

**Live:** [www.varfinnsdet.se](https://www.varfinnsdet.se)

---

## Hur sajten fungerar

### Polling-loopen

En bakgrundstråd körs kontinuerligt och hämtar lagerstatus för alla bevakade läkemedel via [Fass.se](https://fass.se):

1. Apoteksregistret (ca 1 450 apotek med GLN-koder) hämtas från LMV (Läkemedelsverket) vid uppstart och sparas i databasen för omedelbar tillgång vid omstart.
2. Varje polling-cykel kontrollerar lagerstatus för de hårdkodade standardläkemedlen samt alla läkemedel som användare aktivt bevakar.
3. Kontrollen sker i parallella trådar (ett API-anrop per läkemedel) mot Fass pharmacy stock-API:t.
4. Om ett läkemedel går från restnoterat till i lager skickas e-post till alla prenumeranter för det läkemedlet.
5. För att undvika falska notiser vid tillfälliga API-fel krävs att ett nollresultat upprepas två gånger i rad innan ett läkemedel markeras som utgånget.

### Prenumerationsflödet

```
Användaren söker läkemedel
        ↓
Väljer förpackning → klickar Bevaka
        ↓
Fyller i e-post + GDPR-samtycke
        ↓
Bekräftelsemejl skickas (double opt-in)
        ↓
Användaren klickar länken → prenumeration aktiveras (30 dagar)
        ↓
Polling-loopen hittar läkemedlet i lager → e-post skickas
        ↓
Prenumerationen löper ut automatiskt eller avslutas via länk i mejlet
```

Prenumerationer förlängs med 30 dagar via en länk i påminnelsemejlet som skickas 5 dagar innan de löper ut.

---

## Teknisk stack

| Komponent | Val |
|---|---|
| Webbramverk | Flask 3 + Gunicorn (1 worker, 4 trådar) |
| Databas | SQLite med WAL-läge, persistent volym på Railway |
| E-post | [Resend](https://resend.com) |
| Läkemedelsdata | Fass.se CMS-API (inofficiellt) |
| Apoteksregister | LMV (Läkemedelsverket) |
| Hosting | [Railway](https://railway.app) |
| Analys | [Umami](https://umami.is) |

Polling-loopen körs som en daemon-tråd i samma Gunicorn-process. En worker används för att undvika SQLite-konflikter.

---

## Filstruktur

```
├── app.py              # Flask app-factory, routes för /, /og-image.png, /privacy
├── checker.py          # Polling-loop, PRODUCTS-lista, apoteksregister-cache
├── db.py               # SQLite-schema och init
├── fass.py             # Fass.se API-wrapper: sökning, förpackningar, lagerstatus
├── mail.py             # Resend-wrapper och e-postmallar
├── routes/
│   ├── search.py       # GET /api/search, /api/packages, /api/stock/:id
│   ├── subscribe.py    # POST /subscribe, GET /confirm/:token
│   ├── manage.py       # GET /manage/:token — hantera bevakningar
│   ├── extend.py       # GET /extend/:token — förläng 30 dagar
│   ├── unsubscribe.py  # GET /unsubscribe/:token
│   └── log.py          # GET /log — polling-historik
├── templates/          # Jinja2-mallar
└── static/             # Statiska filer (OG-bild m.m.)
```

---

## Databas

SQLite på Railway persistent volym (`/data/medicinstatus.db`).

| Tabell | Innehåll |
|---|---|
| `medications` | Läkemedel med NPL-ID och namn |
| `subscribers` | E-postadresser (soft delete) |
| `subscriptions` | Kopplingen prenumerant ↔ läkemedel, löptid |
| `tokens` | UUID-tokens för confirm/unsubscribe/manage/extend |
| `poll_log` | Polling-historik, rullande 2 000 rader |
| `pharmacy_cache` | Apoteksregistret cachat för snabb uppstart |
| `daily_mail_count` | Daglig e-posträknare mot Resend-gränsen |

---

## Miljövariabler

| Variabel | Beskrivning | Standard |
|---|---|---|
| `RESEND_API_KEY` | API-nyckel från Resend | — |
| `FROM_EMAIL` | Avsändaradress | `noreply@varfinnsdet.se` |
| `SITE_NAME` | Sajt-namn i mallar | `varfinnsdet.se` |
| `SITE_URL` | Publik URL (utan avslutande /) | — |
| `DB_PATH` | Sökväg till SQLite-databasen | `/data/medicinstatus.db` |
| `POLL_INTERVAL` | Minuter mellan pollningar | `2` |
| `CACHE_FILE` | Sökväg för tillstånds-cache | `/data/medicinstatus_cache.json` |

---

## Kör lokalt

```bash
pip install -r requirements.txt
export SITE_NAME="varfinnsdet.se"
export SITE_URL="http://localhost:5000"
flask --app "app:create_app()" run
```

Utan `RESEND_API_KEY` skickas inga mejl — sajten fungerar i övrigt fullt ut.

---

## GDPR

- E-postadresser lagras krypterat i SQLite, aldrig i klartext i loggar.
- Samtycke inhämtas explicit (double opt-in) med stöd av artikel 6(1)(a) och 9(2)(a) GDPR.
- Prenumerationer löper ut automatiskt efter 30 dagar.
- Avregistrering sker via länk i varje mejl — idempotent, kräver inget konto.
- Integritetspolicy: [www.varfinnsdet.se/privacy](https://www.varfinnsdet.se/privacy)

---

## Datakällor

Lagerstatus hämtas från Fass.se i samarbete med Sveriges Apoteksförening. Informationen kan vara fördröjd — kontakta alltid ditt apotek för aktuell status. Varfinnsdet.se är inte kopplat till Fass, LIF eller Sveriges Apoteksförening.
