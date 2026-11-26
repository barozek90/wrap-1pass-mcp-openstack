# mcp-1wrap-openstack

Launcher 1Password dla serwerów MCP. Czyta sekrety przez CLI `op`, przygotowuje
konfigurację (np. `clouds.yaml` + `secure.yaml` dla OpenStacka w prywatnym katalogu
w RAM), a następnie **zastępuje się** właściwym serwerem MCP (`exec`). Klient
rozmawia z serwerem bezpośrednio po stdio, a launcher nic nie wypisuje na stdout.
Wyjątek: [tryb fan-out](#tryb-fan-out) (jeden wpis dla wielu chmur), w którym launcher zostaje
przy życiu jako serwer MCP i na stdout wypisuje wyłącznie wiadomości MCP.

Wymagania: Python >= 3.11 (tylko biblioteka standardowa), zainstalowane `op`
i działające 1Password z włączoną integracją CLI (Settings -> Developer ->
*Integrate with 1Password CLI*).

Wbudowane dostawcy:

| `type`      | co robi |
|-------------|---------|
| `openstack` | item w 1Password = jedna chmura -> `clouds.yaml` + `secure.yaml`, ustawia `OS_CLIENT_*_FILE`, opcjonalnie `OS_CLOUD` i `CLOUD_NAME` |
| `env`       | referencje `op://sejf/item/pole` -> zmienne środowiskowe serwera |
| `item`      | wybrane pola itemu chmury (tego samego, co dla `openstack`) -> zmienne środowiskowe serwera, np. adres instancji VictoriaLogs |

## Użycie w kliencie MCP

Składnia: `mcp-1wrap [opcje] -- POLECENIE [ARGUMENTY...]`. Pinuj **oba** repozytoria do
przejrzanych commitów: launcher widzi wszystkie sekrety, a `@main` oznaczałoby, że
każdy z prawem do pushu je odczyta.

Claude Code (terminal):

```bash
claude mcp add --env OS_CLOUD=example-cloud --transport stdio --scope user \
  openstack -- \
  uvx --from git+https://github.com/barozek90/mcp-1wrap-openstack.git@<HASH_WRAPPERA> \
  mcp-1wrap -- \
  uvx --from git+https://github.com/barozek90/mcp-openstack.git@<HASH_SERWERA> mcp-openstack
```

Po `--env ZMIENNA=wartość` musi stać jeszcze jakiś przełącznik (tu `--transport`) przed nazwą serwera:
nazwa tuż po `--env` byłaby wczytana jako kolejna para `KLUCZ=wartość` i odrzucona.

Aplikacja desktopowa (`claude_desktop_config.json`, klucz `mcpServers`):

```json
"openstack": {
  "command": "uvx",
  "args": [
    "--from", "git+https://github.com/barozek90/mcp-1wrap-openstack.git@<HASH_WRAPPERA>",
    "mcp-1wrap", "--",
    "uvx", "--from", "git+https://github.com/barozek90/mcp-openstack.git@<HASH_SERWERA>",
    "mcp-openstack"
  ],
  "env": { "OS_CLOUD": "example-cloud" }
}
```

Jeśli `uvx` nie jest w PATH klienta GUI, podaj pełną ścieżkę (`type -a uvx`) jako `command`.
Launcher sam uzupełnia `HOME`, `PATH` (z `~/.local/bin`), `XDG_RUNTIME_DIR` i adres DBus, a
nieistniejącą ścieżkę bezwzględną do polecenia serwera zastępuje tym, co znajdzie w PATH.

## Dostawca `openstack`

Itemy wybiera sejf + tag. Tytuł itemu jest nazwą chmury (przepisywany 1:1, w `OS_CLOUD`
dopasowywany bez rozróżniania wielkości liter). Etykiety pól też są porównywane bez
rozróżniania wielkości liter.

| pole | |
|------|--|
| `website` (adres URL itemu) | wymagane, adres Keystone (w razie braku etykiety `website` brany jest główny URL) |
| `username`, `password` | wymagane |
| `region_name` | domyślnie tytuł itemu |
| `user_domain_name` | domyślnie `Default` |
| `project_name`, `project_domain_name` | domyślnie `admin`, `Default` |
| `system_scope` = `all` | scope systemowy; wyklucza pola `project_*` |
| `interface` | domyślnie `public` |
| `identity_api_version` | domyślnie `3` (musi być liczbą) |
| `verify_cert` | `true` (lub brak pola) = certyfikat TLS jest sprawdzany (domyślnie); `false` = certyfikat ignorowany. Dozwolone: `true/false`, `yes/no`, `1/0` (bez rozróżniania wielkości liter) |

`verify_cert = false` zapisuje `verify: false` w `clouds.yaml` i wypisuje ostrzeżenie w podsumowaniu.
Wyłączaj tylko dla zaufanych środowisk (np. certyfikat samopodpisany).

Przy braku pola lub błędnej wartości launcher kończy się z kodem 1 i komunikatem ze
wskazaniem etapu i parametru, np.
`mcp-1wrap: BŁĄD [openstack/item 'EXAMPLE-CLOUD'] brakuje wymaganych pól: website, username`.

Zmienne środowiskowe (pierwszeństwo przed profilem):

| zmienna | znaczenie | domyślnie |
|---------|-----------|-----------|
| `OP_VAULT` | sejf z itemami | `Private` |
| `OP_TAG` | tag itemów (nie jest przekazywana do serwera) | `OS_TAG` |
| `OS_CLOUD` | generuj tylko tę chmurę (bez rozróżniania wielkości liter); bez niej wszystkie z tagiem | - |
| `OP_TIMEOUT` | sekundy na jedno wywołanie `op` | `60` |
| `MCP_1WRAP_PROFILE` | nazwa profilu | wbudowany `openstack` |
| `MCP_1WRAP_DEBUG` | `1` = diagnostyka na stderr | - |

Gdy ustawisz `OS_CLOUD`, w pliku z hasłami znajdzie się tylko ta jedna chmura, a serwer
dostaje dokładną nazwę z itemu w `OS_CLOUD` i `CLOUD_NAME` (tej drugiej używa `mcp-openstack`).

## Dostawca `item`

Zmienne środowiskowe serwera z pól **jednego** itemu chmury: tego samego itemu, tagu i selektorów co przy
`openstack` (`OP_VAULT`, `OP_TAG`, `OS_CLOUD`; tytuł itemu = nazwa chmury, bez rozróżniania wielkości liter).
Dostawca obsługuje jedną chmurę naraz (`OS_CLOUD` albo opcja `cloud`), czyta tylko wskazane pola i niczego więcej
z itemu nie używa ani nie wypisuje. Wzór profilu: [`examples/victorialogs.toml`](examples/victorialogs.toml).

| opcja | |
|-------|--|
| `tag`, `vault`, `cloud` | jak przy `openstack` (zmienne środowiskowe mają pierwszeństwo) |
| `vars` | wymagana tabela: `ZMIENNA = "etykieta"` albo `ZMIENNA = { field = "etykieta", check = "url" }`; etykiety pól bez rozróżniania wielkości liter |
| `allow_hosts` | lista wzorców (np. `*.intra.example`); przy `check = "url"` host musi pasować do któregoś |
| `allow_http` | `true` = `http` także poza loopback (domyślnie dozwolony tylko `https`) |

Każda wartość: białe znaki z brzegów są obcinane, wartość jest niepusta, ma do 2048 znaków i nie zawiera znaków
sterujących. Brak pola, pole puste lub niepoprawna wartość kończą start błędem z nazwą itemu i etykiety (nigdy z wartością).

`check = "url"` przyjmuje tylko zwykły adres punktu końcowego `https://host[:port][/ścieżka]` (`http` wyłącznie dla
loopback, chyba że `allow_http`): bez danych logowania, zapytania i fragmentu, bez `..` w ścieżce (także
zakodowanego), bez domen nie-ASCII (użyj punycode), bez kropki na końcu hosta, bez liczbowego zapisu hosta, bez
portu pustego lub `0` oraz bez adresów IP link-local, nieokreślonych i multicast (także ukrytych w IPv6: mapowane,
6to4, Teredo, NAT64, SIIT; zakres metadanych AWS po IPv6 i znane nazwy hostów metadanych też są odrzucane).
To ochrona przed adresami wpisanymi wprost: nazwa DNS wskazująca na taki adres przejdzie, chyba że ustawisz
`allow_hosts` (zalecane na produkcji). Adresy z `check = "url"` są pokazywane w `--check` i w podsumowaniu (nie mają
danych logowania ani zapytania, ale ścieżka może zawierać dowolny tekst: nie wkładaj do niej tokenów); wartości
`check = "text"` nigdy.

## Tryb fan-out

Gdy profil ma tabelę `[fanout]`, `mcp-1wrap` **nie zastępuje się** serwerem (`exec`), tylko zostaje przy życiu jako
serwer MCP po stdio i obsługuje wiele chmur jednym wpisem klienta. Do każdego narzędzia prawdziwego serwera dokłada
wymagany argument (domyślnie `cloud`) i kieruje wywołanie do osobnego procesu serwera uruchomionego dla tej chmury,
ze zmiennymi, które dostawcy wyznaczają dla niej (np. adres z pola itemu). Klient widzi jeden serwer i jeden zestaw
narzędzi, a agent podaje nazwę chmury tak samo jak przy narzędziach OpenStack. Na stdout idą wyłącznie wiadomości MCP
(jedna na linię), reszta na stderr.

| klucz `[fanout]` | znaczenie |
|------------------|-----------|
| `probe_env` | wymagane: zmienne środowiskowe sondy, która pobiera listę narzędzi serwera; sonda nie łączy się z żadnym adresem (serwer nie sprawdza go przy starcie), więc wystarczy poprawna wartość, np. `http://127.0.0.1:9` |
| `argument` | nazwa dodawanego argumentu (domyślnie `cloud`) |
| `allow_clouds` | lista dozwolonych chmur: ogranicza wywołania, daje `enum` w schemacie i ukrywa w błędach nazwy pozostałych chmur |
| `inherit` | dodatkowe zmienne środowiskowe przekazywane backendom |
| `start_timeout` | sekundy na każdy etap startu backendu i sondy: uzgadnianie połączenia, lista narzędzi (domyślnie `30`, najwyżej `3600`; czas `op` nie wchodzi w ten limit) |
| `call_timeout` | sekundy na jedno wywołanie narzędzia (domyślnie `600`, najwyżej `86400`); zapis do zawieszonego backendu nie podlega temu limitowi, ale zamknięcie bramki kończy go sygnałem |

Zasady działania:

- `initialize` jest obsługiwane od razu, bez 1Password i bez uruchamiania serwera; lista narzędzi pochodzi z sondy
  (jeden krótko żyjący proces, po `initialize` w tle). Przy wersji protokołu spoza `2025-11-25`, `2025-06-18`,
  `2025-03-26`, `2024-11-05` bramka odpowiada `2025-11-25`.
- Chmura jest rozwiązywana (dostawcy, `op`) dopiero przy starcie jej backendu, więc ciepłe wywołania nie dotykają `op`.
  Backend starszy niż 10 minut jest przy następnym wywołaniu restartowany, o ile nie ma w nim żądań w toku (zmiana
  pola w itemie staje się wtedy widoczna). Zwykle działa do 8 backendów naraz (najdawniej użyty bezczynny ustępuje
  miejsca; przy równoległym starcie wielu chmur liczba chwilowo bywa większa). Bramka pamięta do 64 różnych nazw chmur.
- Błąd wyznaczenia chmury (nazwa spoza listy, brak pola, zła wartość) lub startu serwera jest pamiętany 15 s dla tej
  chmury. Do globalnego licznika, który po 5 awariach w ciągu 30 s wstrzymuje kolejne starty, wchodzą tylko awarie
  `op` i startu serwera, więc literówki w nazwach chmur nie blokują pozostałych.
- Poza zmiennymi z `inherit` i zmiennymi od dostawców backend dostaje **tylko**: `PATH`, `HOME`, `USER`, `LOGNAME`,
  `LANG`, `LANGUAGE`, `LC_*`, `TZ`, `TMPDIR`, zmienne proxy (przekazywane tak, jak są, więc mogą zawierać dane
  logowania do proxy), `SSL_CERT_FILE`, `SSL_CERT_DIR`. W szczególności nie dostaje `OP_*`, `OS_*`, `AWS_*`
  ani `SSH_AUTH_SOCK`, chyba że wskażesz je w `inherit` albo ustawi je dostawca.
- Obsługiwane są `ping`, anulowanie (`notifications/cancelled`) i `notifications/progress`. Po zamknięciu stdin,
  SIGTERM albo SIGINT bramka w ok. 1-2 s kończy backendy (grupa procesów) i od razu kończy proces, nie czekając na
  wątki zablokowane w `op` ani w starcie backendu (backend uruchomiony w tej chwili jest zamykany po starcie). Gdy
  klient przestanie czytać wyjście, bramka kończy się jak po SIGTERM. Po `kill -9` bramki backend kończy się sam,
  bo traci stdin.
- Błędy bramki (nieznana chmura z listą dostępnych, brak pola, nieudany start, limit czasu) wracają jako wynik
  narzędzia z `isError`. Każde wywołanie, także odrzucone, zostawia na stderr linię: chmura, narzędzie, wynik
  (bez treści zapytania).

Ograniczenia: brak zasobów i promptów serwera (narzędzie `documentation` działa), brak batchy JSON-RPC, brak
rewizji MCP `2026-07-28` (bez `initialize`; `server/discover` dostaje `-32601`), argument `cloud` jest zawsze wymagany,
pierwsze wywołanie chmury może wywołać odblokowanie 1Password. Treść logów zwracana przez narzędzia jest daną
niezaufaną (wstrzykiwanie poleceń przez linie logów): `instructions` ostrzega model, a `allow_clouds` zawęża zasięg.
Jedno zezwolenie na `mcp__<serwer>__*` obejmuje wszystkie chmury, więc nie zatwierdzaj automatycznie narzędzi
modyfikujących OpenStack w tej samej sesji. Zmiana hasha wrappera w configu klienta wymaga przeglądu diffu `fanout.py`.

`mcp-1wrap --check` w trybie fan-out: bez `OS_CLOUD` wypisuje profil, polecenie i raport pokrycia (dla każdej chmury
`ok` albo brakujące pole lub niepoprawna wartość; kilka wywołań `op` na chmurę; przy `allow_clouds` tylko dozwolone
chmury) i kończy się kodem 0 tylko wtedy, gdy każda chmura jest gotowa. Z `OS_CLOUD` sprawdza jedną chmurę jak zawsze.
Jeśli któreś narzędzie serwera ma już argument o nazwie `cloud`, `tools/list` kończy się błędem: zmień `argument`.

## Przykład: VictoriaLogs dla wielu chmur

Serwer [`mcp-victorialogs`](https://github.com/VictoriaMetrics/mcp-victorialogs) jest napisany w Go i czyta adres
instancji ze zmiennej `VL_INSTANCE_ENTRYPOINT`, więc jeden jego proces obsługuje jedną instancję. Adresy trzymasz
w polach itemów chmur (tytuł itemu = nazwa chmury, ta sama co dla `openstack`), a jeden wpis klienta obsługuje
wszystkie chmury dzięki trybowi fan-out.

1. Adres w itemie, dla każdej chmury z działającym endpointem:

   ```bash
   op item edit "EXAMPLE-CLOUD" 'vlogs_url[url]=https://vlogs.example'
   ```

2. Adres musi wskazywać **endpoint zapytań** VictoriaLogs (pojedyncza instancja albo `vlselect`, ewentualnie `vmauth`
   przed nimi), a nie agenta zbierającego logi (`vlagent`). Sprawdzenie (nie wypisuje treści logów):

   ```bash
   curl -sS -m 10 -o /dev/null -w '%{http_code}\n' -d 'query=_time:1s' -d 'limit=1' https://vlogs.example/select/logsql/query
   ```

   `200` = endpoint zapytań, `401`/`403` = wymaga uwierzytelnienia (token podawaj dostawcą `env`, nigdy polem
   tekstowym itemu), `404` = to nie jest endpoint zapytań.

3. Serwer `mcp-victorialogs`. `uvx` uruchamia pakiety Pythona, więc **nie uruchomi** go (to program w Go, bez pakietu
   Pythona). Zainstaluj go raz, zawsze w przypiętej wersji (nigdy `@latest`); `harden()` dokłada `~/.local/bin`
   do PATH, więc wystarczy polecenie `mcp-victorialogs`:

   - binarka z [Releases](https://github.com/VictoriaMetrics/mcp-victorialogs/releases) (pełny, oficjalny build) do
     `~/.local/bin`; zapisz jej sha256 i porównaj z `checksums.txt`, jeśli jest w wydaniu (to sprawdza integralność,
     nie autentyczność);
   - albo `GOBIN="$HOME/.local/bin" go install github.com/VictoriaMetrics/mcp-victorialogs/cmd/mcp-victorialogs@vX.Y.Z`
     (moduł weryfikuje sumdb; zapisz sha256 binarki; sprawdź, czy działa narzędzie `documentation`);
   - albo obraz OCI przypięty digestem jako polecenie serwera:
     `docker run -i --rm --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 100 -e VL_INSTANCE_ENTRYPOINT ghcr.io/victoriametrics/mcp-victorialogs@sha256:<digest>`
     (DNS hostów wewnętrznych z kontenera może nie działać; nie używaj `--network host` tylko dla wygody; backend
     musi dostać `DOCKER_HOST`, jeśli go potrzebujesz: `inherit = ["DOCKER_HOST"]`);
   - `go run ...@vX.Y.Z` tylko do eksperymentów: pierwszy start pobiera toolchain i kompiluje, co trwa dłużej niż
     timeouty klientów.

4. Profil `~/.config/mcp-1wrap/profiles/victorialogs.toml` według [`examples/victorialogs.toml`](examples/victorialogs.toml)
   i raport pokrycia: `mcp-1wrap --profile victorialogs --check -- mcp-victorialogs`.

5. Jeden wpis klienta, bez `OS_CLOUD`, z tym samym przypiętym wrapperem co przy `openstack`:

   ```bash
   claude mcp add --transport stdio --scope user \
     victorialogs -- \
     uvx --from git+https://github.com/barozek90/mcp-1wrap-openstack.git@<HASH_WRAPPERA> \
     mcp-1wrap --profile victorialogs -- mcp-victorialogs
   ```

   ```json
   "victorialogs": {
     "command": "uvx",
     "args": [
       "--from", "git+https://github.com/barozek90/mcp-1wrap-openstack.git@<HASH_WRAPPERA>",
       "mcp-1wrap", "--profile", "victorialogs", "--", "mcp-victorialogs"
     ]
   }
   ```

Wpis na jedną chmurę zamiast fan-outu: ten sam profil bez tabeli `[fanout]` i `OS_CLOUD` w configu klienta
(`--env OS_CLOUD=EXAMPLE-CLOUD`, po nim jakiś przełącznik przed nazwą serwera, jak w przykładzie `openstack` wyżej).

## Profile (TOML)

`~/.config/mcp-1wrap/profiles/<nazwa>.toml` (`$XDG_CONFIG_HOME` jest respektowany),
wybierany przez `--profile <nazwa>`, `--profile-file <ścieżka>` lub `MCP_1WRAP_PROFILE`.
Profil zawiera nazwy sejfów, tagów i referencje, **nigdy sekrety**.

```toml
vault   = "Private"          # domyślny sejf
timeout = 60                 # sekundy na wywołanie 'op'
unset   = ["OP_TAG"]         # zmienne, których serwer nie dostanie

[[provider]]
type = "openstack"
tag  = "OS_TAG"
# vault = "Inny"             # nadpisuje sejf profilu
# cloud = "example-cloud"     # jak OS_CLOUD (zmienna środowiskowa wygrywa)
# cloud_name_var = "CLOUD_NAME"   # "" wyłącza eksport nazwy chmury

[[provider]]
type = "env"
[provider.vars]
GITLAB_TOKEN = "op://Private/gitlab/token"
```

Dwóch dostawców nie może ustawiać tej samej zmiennej (błąd z jej nazwą). Bez profilu działa
wbudowany `openstack`.

## Test ręczny

```bash
mcp-1wrap --check                  # przygotuj wszystko, wypisz podsumowanie (bez sekretów), nie startuj serwera
mcp-1wrap --check --debug          # + diagnostyka środowiska (HOME, PATH, XDG_RUNTIME_DIR, lokalizacja op/uvx)
OS_CLOUD=example-cloud mcp-1wrap --check
mcp-1wrap -- env | grep -E '^(OS_|CLOUD_NAME)'    # co zobaczy serwer
mcp-1wrap --profile victorialogs --check          # profil z [fanout]: raport pokrycia wszystkich chmur
OS_CLOUD=example-cloud mcp-1wrap --profile victorialogs --check    # jedna chmura (adres z pola, bez sekretów)
```

Z poziomu repo bez instalacji: `uv run mcp-1wrap --check` albo
`PYTHONPATH=src python -m mcp_1wrap --check`.

## Bezpieczeństwo

- Hasła trafiają jako jawny tekst do plików `0600` w katalogu `0700` w RAM
  (`$XDG_RUNTIME_DIR`, potem `/run/user/<uid>`, potem `/dev/shm`; dopiero w ostateczności `/tmp`
  z ostrzeżeniem). Katalog musi należeć do Ciebie, nie być symlinkiem i nie być otwarty dla
  innych, inaczej launcher przerywa pracę. Zapis jest atomowy, a nazwa katalogu zależy od
  sejfu, tagu i wybranej chmury, więc równoległe instancje (terminal + aplikacja) nie psują sobie plików.
- `op` jest wołane z timeoutem i z `stdin=/dev/null`, a komunikaty błędów nie zawierają wartości pól.
- Zmienne `OS_*` inne niż selektory (`OS_CLOUD`, `OS_CLIENT_*`) powodują, że openstacksdk pokazuje
  fikcyjną chmurę `envvars`; launcher usuwa własne pomocnicze zmienne (`OP_TAG`) przed uruchomieniem serwera.
- Pinuj commit wrappera i serwera w configu klienta. Aktualizacja = podmiana hasha po przeglądzie zmian.
- W trybie fan-out launcher żyje przez całą sesję klienta i przyjmuje wywołania od modelu: argument `cloud` służy
  wyłącznie jako klucz wyszukiwania (żadna jego wartość nie trafia do powłoki, ścieżki ani argumentu `op`), backendy
  dostają tylko dozwolone zmienne środowiskowe, a liczba wywołań, procesów i prób startu jest ograniczona.

## Rozwój

```bash
uv sync
uv run pytest
```

Testy używają atrapy `op` (plik wykonywalny generowany w testach), więc nie wymagają 1Password.
W danych testowych używaj tylko placeholderów (`EXAMPLE-CLOUD`, `*.example`).
