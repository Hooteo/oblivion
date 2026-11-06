# privacy-sweep

Alternativa open source e self-hosted a Incogni / DeleteMe. Invia ai data broker richieste di cancellazione dei dati personali (art. 17 GDPR, più opposizione art. 21 e, per gli USA, richiesta CCPA di delete/opt-out), legge le risposte e ripete tutto in automatico ogni 3 o 6 mesi.

- **~1000 broker inclusi**: registro ufficiale dei data broker della California (CPPA) + database di [datarequests.org](https://www.datarequests.org) (categorie pubblicità, indirizzi, agenzie di credito). Si aggiorna dalla dashboard o automaticamente ogni mese tramite GitHub Actions.
- **Più caselle email**: ogni casella invia a nome proprio e chiede la cancellazione dei dati legati a quell'indirizzo. Si configurano dalla dashboard (con preset per Gmail, Outlook, iCloud, Yahoo, Libero, Virgilio, Aruba, Tiscali, Fastmail).
- **Dashboard**: a chi hai scritto, chi ha risposto, chi ha confermato, chi chiede una verifica, chi non ha risposto, rimbalzi.
- **Lettura risposte via IMAP** (sola lettura): abbina le risposte alle richieste (Message-ID, dominio, nome del broker) e le classifica: *confermata*, *nessun dato*, *serve un tuo intervento*, *rifiutata*, *rimbalzo*.
- **Solleciti automatici** dopo 35 giorni senza risposta, poi chiusura come "nessuna risposta".
- **Invio graduale** (limite giornaliero per casella, pausa tra invii, fascia oraria) per non farsi bloccare dal provider.

## Avvio rapido

```bash
git clone https://github.com/Hooteo/privacy-sweep.git && cd privacy-sweep
cp .env.example .env    # imposta DASHBOARD_PASSWORD e SECRET_KEY
mkdir -p state && sudo chown 1000:1000 state   # il container gira come utente 1000
docker compose up -d    # dashboard su http://IP:8095  (utente admin)
```

Genera la `SECRET_KEY` con:

```bash
docker run --rm python:3.13-slim sh -c "pip -q install cryptography && python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'"
```

Senza `SECRET_KEY` la chiave viene creata in `state/secret.key`. Database e impostazioni stanno in `state/`: è la cartella da salvare nei backup. Le password SMTP/IMAP sono cifrate con questa chiave: se la perdi devi reinserirle.

## Primo utilizzo

1. **Caselle email → Aggiungi casella**. Scegli il provider per compilare server e porte, inserisci una **password per le app** (Gmail/Yahoo/iCloud la richiedono, serve la verifica in due passaggi attiva).
2. **Prova connessione**, poi **Mandami una richiesta di prova** per vedere la mail che riceveranno i broker.
3. In **Impostazioni** controlla il testo (in inglese, perché va a broker di tutto il mondo).
4. In **Broker** controlla quali sono attivi. Disattivati di default: broker rilevanti solo per altri paesi (es. solo Germania), agenzie di credito, broker senza email.
5. Spunta **Avvia subito la prima campagna** o premi **Avvia campagna ora**. Le campagne successive partono da sole ogni 3 o 6 mesi.

## Come funziona

| Fase | Cosa succede |
|---|---|
| Campagna | una richiesta in coda per ogni broker attivo |
| Invio | max *N* mail al giorno per casella (default 80), una ogni ≥45 s, tra le 8 e le 21 |
| Risposte | IMAP ogni 15 min sulle cartelle indicate (es. `INBOX,[Gmail]/Spam`) |
| Sollecito | dopo 35 giorni senza risposta, nello stesso thread |
| Chiusura | 30 giorni dopo il sollecito → "nessuna risposta" (puoi segnalarlo al Garante) |
| Rimbalzi | dopo 2 rimbalzi il broker viene disattivato |

Lo stato di una richiesta si può sempre correggere a mano. Da quel momento le risposte non lo cambiano più, finché non premi "Torna automatico".

Le mail da domini di broker che non riesce ad abbinare (es. inviate da piattaforme condivise come OneTrust o Zendesk) finiscono in **Da abbinare**. Le altre mail della tua casella vengono ignorate e non salvate.

## Limiti da conoscere

- Molti broker americani rispondono con un link a un **modulo web** o chiedono un **documento d'identità**: queste richieste finiscono in "Serve un tuo intervento" e vanno completate a mano.
- Il CCPA tutela i residenti in California. Ai broker USA la richiesta si basa sull'art. 3(2) GDPR: alcuni la onorano, altri rispondono che vale solo per i californiani ("Rifiutata").
- Per il telemarketing in Italia iscriviti anche al [Registro Pubblico delle Opposizioni](https://www.registrodelleopposizioni.it): è gratuito e copre fissi e cellulari.
- Outlook/Hotmail stanno dismettendo l'accesso SMTP/IMAP con password: se il test fallisce usa un'altra casella.

## Endpoint di monitoraggio

`GET /healthz` (senza autenticazione) → `{"status":"ok"|"warn","identity_errors":[...]}`.

## Sviluppo

```bash
pip install -r requirements.txt pytest
pytest -q
DATA_DIR=./data uvicorn app.main:app --reload
python scripts/build_brokers.py   # rigenera data/brokers.json
```

## Fonti dati e licenze

- California Privacy Protection Agency, [Data Broker Registry](https://cppa.ca.gov/data_broker_registry/), dati pubblici.
- [datenanfragen/data](https://github.com/datenanfragen/data), CC0 1.0.

Codice sotto licenza MIT. Non è consulenza legale.
