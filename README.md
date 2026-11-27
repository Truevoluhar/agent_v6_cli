# Agent CLI

Samostojen lokalni agent za Windows, macOS in Linux. Namesti se na uporabnikov računalnik; ne teče v Dockerju in za delo ne kliče agent-api. Za modele uporablja obstoječi Bifrost, za uporabnike/prijavo obstoječi Postgres. **API in njegov Docker Compose ostajata nespremenjena.**

Po dogovorjeni ločitvi CLI uporablja iste uporabnike (`app_users`, `auth_sessions`), svoje pogovore, korake in spomine pa hrani v shemi `agent_cli` iste baze. Pogovori/spomini CLI se ne pojavijo v obstoječem spletnem GUI-ju. CLI pogovor lahko nadaljuje isti uporabnik z drugega računalnika; lokalne datoteke in procesi se med računalniki ne prenašajo.

## Namestitev za uporabnika

Potreben je Python 3.11 ali novejši. Namestljiv wheel je v `dist/zpiz_agent_cli-0.1.0-py3-none-any.whl`. Wheel vsebuje CLI in SQL pogodbo, ne uporabniških konfiguracij, dokumentov ali obstoječega agentovega izvora. Python odvisnosti se namestijo za ustrezen OS.

Če je pipx že nameščen:

```bash
pipx install ./dist/zpiz_agent_cli-0.1.0-py3-none-any.whl
```

Windows PowerShell, brez pipx:

```powershell
py -m venv "$env:LOCALAPPDATA\AgentCLI\venv"
& "$env:LOCALAPPDATA\AgentCLI\venv\Scripts\python.exe" -m pip install .\dist\zpiz_agent_cli-0.1.0-py3-none-any.whl
& "$env:LOCALAPPDATA\AgentCLI\venv\Scripts\Activate.ps1"
agent-cli --help
```

macOS/Linux, brez pipx:

```bash
python3 -m venv ~/.local/share/agent-cli-venv
~/.local/share/agent-cli-venv/bin/python -m pip install ./dist/zpiz_agent_cli-0.1.0-py3-none-any.whl
~/.local/share/agent-cli-venv/bin/agent-cli --help
```

Za razvoj iz te mape: `python -m pip install -e .`. CLI ne uvaža ničesar iz `agent_v6_deep` ali drugih sosednjih projektov.

## Enkratna priprava baze pri administratorju

Najprej mora obstajati obstoječa aplikacijska baza z `public.app_users` in `public.auth_sessions`. CLI ne ustvari/resetira nobenega aplikacijskega uporabnika, skrbniškega računa ali gesla. Administrator v svojem okolju nastavi `AGENT_CLI_DATABASE_URL` na administratorsko povezavo in požene:

```bash
agent-cli db-init
```

To idempotentno doda samo shemo `agent_cli`, njene tabele, omejeno SQL funkcijo `agent_cli.rpc` in skupinsko vlogo `agent_cli_client` brez možnosti prijave. Izvor SQL je v `src/agent_cli/schema.sql`.

Administrator nato pripravi ločen SQL LOGIN račun za CLI odjemalce, npr. prek svojega PostgreSQL administratorskega orodja:

```sql
CREATE ROLE cli_user LOGIN PASSWORD 'NASTAVI_NOVO_GESLO';
GRANT agent_cli_client TO cli_user;
GRANT CONNECT ON DATABASE deepagents TO cli_user;
```

Končnim uporabnikom razdelite DSN tega omejenega SQL računa. Ne razdeljujte administratorskega DSN ali Dockerjevega računa lastnika baze. Vsi se nato dodatno prijavijo s svojim obstoječim aplikacijskim uporabniškim imenom in geslom. SQL račun ima dostop samo do preverjenega RPC; nima neposrednega SELECT nad gesli, sejami ali CLI tabelami. Vsaka operacija preveri uporabnikovo sejo in lastništvo. Pomembno: dodatni GRANT-i, ki jih administrator dodeli SQL LOGIN računu izven te nastavitve, niso del te omejitve.

CLI trenutno podpira obstoječo **lokalno** prijavo s `password_plain` ter lastni veljavni GUID iz `auth_sessions`; gesla samo primerja po sedanji pogodbi in jih ne hrani lokalno. Zunanjega SSO/verifier načina ne posnema in ne zaobide; za takšno namestitev je potrebna ločena integracija prijave. Administratorski odvzem/deaktivacija uporabnika in preklic seje veljata tudi za CLI. `logout` prekliče samo sejo tega CLI-ja.

Za oddaljene računalnike morata biti Postgres in Bifrost dosegljiva po naslovu strežnika; Dockerjeva imena `postgres`, `agent-api` ali `host.docker.internal` na tujem uporabnikovem računalniku niso ustrezen naslov. Administrator pripravi omrežni dostop in TLS/VPN; `sslmode` za Postgres se nastavi v DSN. Obstoječe Compose konfiguracije ta projekt ne spreminja.

## Nastavitev in prijava

```bash
agent-cli configure
agent-cli login
agent-cli doctor
agent-cli models
```

`configure` vpraša za Postgres DSN, Bifrost base URL, model, ključ in izbirni model za vid. Base URL je npr. `http://STREZNIK:8080/openai`, **ne** Bifrostova nadzorna plošča ali končni `/chat/completions` URL. Uporabite točno ime modela, ki obstaja v vašem Bifrostu. Vid potrebuje model, ki podpira slike; besedilni model sam ne postane OCR/vision model.

Konfiguracija je v uporabniški mapi, ki jo CLI izpiše ob nastavitvi (`platformdirs`, `config.json`). Vzorec vseh ključev je `config.example.json`. Bifrost in Postgres poverilnice ostanejo v tej uporabniški konfiguraciji; na POSIX imajo datoteke pravice 0600. Lastni prijavni GUID se shrani v OS credential store, kadar je na voljo; sicer v zasebno uporabniško datoteko. Na Windows veljajo uporabniške ACL pravice profila. Geslo aplikacijskega uporabnika se ne shranjuje. Za headless okolje lahko z `AGENT_CLI_TOKEN_STORE=file` izberete datotečno shrambo seje.

Okoljske spremenljivke z večjo prednostjo od konfiguracije:

| Spremenljivka | Namen |
|---|---|
| `AGENT_CLI_DATABASE_URL` | DSN za isto obstoječo aplikacijsko bazo, z omejeno CLI SQL vlogo. |
| `BIFROST_BASE_URL` | Bifrost OpenAI-compatible base URL. |
| `BIFROST_MODEL` | Model. |
| `BIFROST_API_KEY` | Bifrostov ključ za inference; ni skrbniški ključ nadzorne plošče. |
| `BIFROST_MCP_ENABLED` | Vklop MCP orodij (`true`/`false`). |
| `BIFROST_MCP_URL` | Bifrost Streamable HTTP MCP naslov, npr. `http://STREZNIK:8080/mcp`. |
| `BIFROST_MCP_API_KEY` | Izbirni Bearer ključ za MCP, ločen od ključa za inference. |
| `AGENT_CLI_CONFIG_DIR` | Izbirna mapa osebne konfiguracije. |
| `AGENT_CLI_STATE_DIR` | Izbirna mapa lokalnih logov, opravil, velikih rezultatov in kazalcev pogovorov. |

`.env` iz map drugih servisov se ne bere samodejno. Za večji odgovor prilagodite `max_output_tokens`; za vizualno branje `vision_output_tokens`. `context_tokens` naj ustreza dejanskemu modelu. `thinking_control` je privzeto `chat_template_kwargs` za Bifrost/vLLM, z izklopljenim razmišljanjem. Pri drugih ponudnikih izberite `none` oziroma `reasoning_effort`, če to konkretni model podpira. Lasten TLS CA certifikat nastavite z absolutno potjo v `ca_file`; preverjanje TLS je privzeto vključeno.

## Zaženite ga v dejanski delovni mapi

```bash
cd /pot/do/projekta
agent-cli
```

Ali izrecno:

```bash
agent-cli --root /pot/do/projekta chat "Preglej projekt, popravi napako in zaženi ustrezne teste."
agent-cli chat --thread ID_POGOVORA "Nadaljuj; najprej preveri trenutno stanje datotek."
agent-cli chat --new --json "Pripravi kratek povzetek projekta."
```

Windows, za delovni kontekst celotnega C diska:

```powershell
Set-Location C:\
agent-cli
# ali:
agent-cli --root C:\
```

Ni kopiranja v strežniški workspace. Datotečna orodja uporabljajo izbrano mapo in podmape, tudi Unicode poti, ter preverijo `..` in cilje simbolnih povezav. Začetna mapa shella je v tej mapi. **Nativni shell ni OS-peskovnik:** izvaja se z dejanskimi pravicami uporabnika in lahko z ukazom dostopa tudi drugam, če OS to dovoljuje. To ni trd varnostni obroč okoli mape. Zagon na C:\ ne dodeli dodatnih administratorskih pravic; sistemske zaščite, UAC, sudo in pravice datotek še vedno veljajo. CLI sam ne dviguje privilegijev in ne potrebuje Dockerja, Bash na Windowsu pa ni predpostavljen.

## Delo v terminalu

Interaktivni ukazi: `/help`, `/new`, `/threads`, `/use UUID`, `/memory`, `/jobs`, `/model NAME`, `/exit`. `/model` spremeni model trenutne seje; trajno nastavitev spremeni `configure`. Prikaz sproti pove fazo in ime orodja; terminalski spinner ostane aktiven med čakanjem na model/orodje. Ctrl+C prekine trenutni agentski obrat, ne ustavi samodejno že zagnanih lokalnih procesov. Po spremembi delovne mape se privzeto odpre lasten pogovor te mape; `--thread` izrecno nadaljuje izbrani pogovor.

Dodatni ukazi:

```bash
agent-cli threads
agent-cli history THREAD_ID
agent-cli turns THREAD_ID
agent-cli turn THREAD_ID TURN_ID
agent-cli memory
agent-cli memory --set-file preferences.txt
agent-cli process
agent-cli process JOB_ID
agent-cli process JOB_ID --stop --force
agent-cli whoami
agent-cli logout
```

## Lokalna orodja

- Datoteke: paginirano branje besedila/binarne vsebine, SHA256, atomski zapis, natančno urejanje brez dvoumnih zamenjav, kopiranje, premikanje, brisanje, mape in iskanje. Urejanje ohrani CRLF in obstoječi način dovoljenj; `encoding` omogoča tudi CP1250/UTF-16 in druge Pythonove kodne nabore; `expected_sha256` omogoča preverjanje sprememb pred zapisom. Simbolno povezavo pri brisanju odstrani kot povezavo, ne njene ciljne datoteke.
- Shell: Bash/sh oziroma PowerShell/pwsh/cmd. Ohrani uporabniški PATH; `python` iz shella je uporabnikov/projektni Python, ne nujno interpreter namestitve CLI-ja. Poljubni ukazi, skripte in namestitve paketov so možni v okviru pravic tega uporabnika.
- Procesi: ločen lokalni nadzorni proces, stdin, EOF, eksplicitno ustavljanje drevesa, ločena stdout/stderr in dejanska izhodna koda. Izvajanje nima avtomatskega časovnega limita. `wait_seconds` do 30 s omejuje samo čakanje na odgovor orodja. Ukaz za strežnik zaženite v ospredju prek tega orodja; dodatni `&` ni potreben. Delavski proces preživi izhod iz CLI-ja in zapisuje končno stanje; po rebootu/nepričakovanem izhodu stanje ni prikazano kot lažen uspeh.
- Terminal: `terminal=true` zagotovi Unix PTY ali Windows ConPTY prek pywinpty; stdout/stderr sta tedaj združena. Agent lahko pošilja nadaljnji vnos in krmilne znake. To ni obljuba avtomatizacije vsakega grafičnega programa; na voljo pa so nativni shell in uporabnikova sistemska orodja.
- Slike: lokalno odpiranje, EXIF orientacija, večstranski TIFF, izbira okvirja, crop in vizualna razpoznava prek izbranega Bifrost modela.
- PDF: izluščitev besedila, šifrirani PDF-ji z geslom, paginacija in dejanski prikaz izbranih strani za vizualno branje/OCR modela. Native rendering se prilagodi zelo velikim stranem in navede dejanski DPI; strani, ki niso bile pregledane, niso navedene kot prebrane.
- Veliki rezultati: poln rezultat ostane v lokalnem uporabniškem stanju, model dobi označen predogled in `artifact_id`; celoto lahko prebere z `read_tool_result`. Ohranitev izhoda ni omejena na predogled.
- Spomin: Postgresov uporabniški spomin z verzijskim preverjanjem, ki prepreči tiho prepisovanje z drugega računalnika.

CLI podpira tudi Bifrostova MCP orodja prek Streamable HTTP, enako kot `agent_v6_deep`. Vklopite jih z `agent-cli configure` ali v uporabniškem `config.json` nastavite:

```json
{
  "bifrost_mcp_enabled": true,
  "bifrost_mcp_url": "http://STREZNIK:8080/mcp",
  "bifrost_mcp_api_key": "VAS_MCP_KLJUC"
}
```

To so dodatna polja obstoječe konfiguracije. Ključ je izbiren, če ga MCP vmesnik ne zahteva. Privzeto je MCP izključen. `agent-cli doctor` preveri povezavo in izpiše odkrite opise orodij. Ob zagonu pogovora CLI pridobi celoten seznam MCP orodij in jih ponudi modelu skupaj z lokalnimi; imena `bifrost_…` preprečijo prekrivanje z lokalnimi orodji. Klici gredo prek MCP na strežnik in se beležijo med korake pogovora; veliki rezultati uporabljajo isti predogled in `read_tool_result`. Napake orodij se vrnejo modelu, neuspešna začetna povezava pa ustavi zagon pogovora. Klici se ne ponavljajo samodejno. MCP uporablja `verify=False`, skladno z interno Bifrost politiko v `agent_v6_deep`; nastavitev `verify_tls` velja za modelsko povezavo.

Pri modelskih zahtevah ostaneta prazna filtra `x-bf-mcp-include-clients` in `x-bf-mcp-include-tools`: model dobi izrecno registrirane sheme, CLI pa izvede in zabeleži klice prek MCP. Tako prehod sam ne dodaja ali izvaja dodatnih orodij mimo CLI-jeve zanke. Lokalna datotečna in shell orodja še vedno delujejo na uporabnikovem računalniku; MCP orodja delujejo v okolju svojega strežnika.

## Pogovori, prekinitve in obseg

Pogovori, koraki in spomin ostanejo v Postgresu. Lokalna opravila, logi, datoteke in veliki rezultati ostanejo na računalniku. Baza vsebuje tudi vsebino pogovorov in rezultate orodij, zato njena hramba ni zgolj seznam ID-jev. Orodja za slikovno prepoznavo pošljejo izbrane slike/strani prek Bifrosta. CLI lokalnih datotek ne sinhronizira v spletni workspace.

Za en račun je v CLI dovoljen en aktiven agentski obrat, z DB zaklepom in heartbeatom. Po izgubi odjemalca lease poteče po petih minutah; naslednji zagon označi star obrat kot prekinjen. Pred novimi orodji preveri veljavnost seje/obrata. Podatkovne zahteve imajo timeout; to ne omejuje trajanja lokalnega ukaza. Prekinjene/nepotrjene akcije se ne ponavljajo avtomatsko. `turn` prikaže shranjene korake za pregled, nato se naloga nadaljuje z novo uporabniško zahtevo in preverjanjem stanja.

Pretekli pogovori se pred prekoračitvijo konteksta povzamejo; celotni ostanejo v bazi. Če trenutni obrat sam preseže proračun konteksta, CLI vrne razumljivo napako. `max_tool_rounds` je nastavljiv; `0` pomeni brez omejitve števila krogov orodij. Nepopolni odgovori z `finish_reason=length` ne sprožijo delno prebranih ukazov. Prijavne seje v bazi veljajo 12 ur; preklic seje ali deaktivacija uporabnika zahteva novo veljavno prijavo.

## Preverjanje in distribucija

```bash
python -m unittest discover -s tests -v
python tests/integration_database.py
python -m pip wheel --no-deps --wheel-dir dist .
```

Integracijski preizkus zažene lasten začasen Postgres 17, ustvari testne uporabnike/vloge in bazo odstrani. Ne bere obstoječe baze, sej ali poverilnic drugih servisov. Preveri omejeno SQL vlogo, prijavo po obstoječi pogodbi, lastništvo pogovorov, lease, spomin, preklic in celoten nameščen CLI podproces z lokalnim nadomestnim Bifrost HTTP strežnikom ter dejanskimi datotečnimi/shell orodji. To ni preizkus natančnosti živega LLM-ja ali OCR-ja.

V tej seji so izvedeni nativni macOS testi in realni Postgres preizkus. `.github/workflows/native-tests.yml` je pripravljen za Windows/macOS/Linux ter Python 3.11/3.12, vendar priprava workflowa ne pomeni, da je Windows zagon že potrjen. Objavite ga v korenu samostojnega repozitorija tega paketa, če želite te CI zagone. Popolne odsotnosti napak, vseh formatov, podpore vsakemu GUI programu ali dostopa mimo OS dovoljenj ne obljubljamo.

Reference za protokole: [Bifrost MCP filtriranje](https://docs.getbifrost.ai/mcp/filtering), [Psycopg namestitev](https://www.psycopg.org/psycopg3/docs/basic/install.html), [Windows terminalski vmesnik pywinpty](https://github.com/andfoy/pywinpty).
