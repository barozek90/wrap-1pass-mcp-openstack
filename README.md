# mcp-1wrap-openstack

Launcher 1Password dla serwerów MCP. Czyta sekrety przez CLI `op`, przygotowuje
konfigurację (np. `clouds.yaml` + `secure.yaml` dla OpenStacka w prywatnym katalogu
w RAM), a następnie **zastępuje się** właściwym serwerem MCP (`exec`). Klient
rozmawia z serwerem bezpośrednio po stdio, a launcher nic nie wypisuje na stdout.

Wymagania: Python >= 3.11 (tylko biblioteka standardowa), zainstalowane `op`
i działające 1Password z włączoną integracją CLI (Settings -> Developer ->
*Integrate with 1Password CLI*).

Wbudowane dostawcy:

| `type`      | co robi |
|-------------|---------|
| `openstack` | item w 1Password = jedna chmura -> `clouds.yaml` + `secure.yaml`, ustawia `OS_CLIENT_*_FILE`, opcjonalnie `OS_CLOUD` i `CLOUD_NAME` |
| `env`       | referencje `op://sejf/item/pole` -> zmienne środowiskowe serwera |

## Użycie w kliencie MCP

Składnia: `mcp-1wrap [opcje] -- POLECENIE [ARGUMENTY...]`. Pinuj **oba** repozytoria do
przejrzanych commitów: launcher widzi wszystkie sekrety, a `@main` oznaczałoby, że
każdy z prawem do pushu je odczyta.

Claude Code (terminal):

```bash
claude mcp add --transport stdio --scope user \
  --env OS_CLOUD=example-cloud \
  openstack -- \
  uvx --from git+https://github.com/barozek90/mcp-1wrap-openstack.git@<HASH_WRAPPERA> \
  mcp-1wrap -- \
  uvx --from git+https://github.com/barozek90/mcp-openstack.git@<HASH_SERWERA> mcp-openstack
```

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

Przy braku pola lub błędnej wartości launcher kończy się z kodem 1 i komunikatem ze
wskazaniem etapu i parametru, np.
`mcp-1wrap: BŁĄD [openstack/item 'EXAMPLE-CLOUD'] brakuje wymaganych pól: website, username`.

Zmienne środowiskowe (pierwszeństwo przed profilem):

| zmienna | znaczenie | domyślnie |
|---------|-----------|-----------|
| `OP_VAULT` | sejf z itemami | `Private` |
| `OS_TAG` | tag itemów (nie jest przekazywana do serwera) | `OS_TAG` |
| `OS_CLOUD` | generuj tylko tę chmurę (bez rozróżniania wielkości liter); bez niej wszystkie z tagiem | - |
| `OP_TIMEOUT` | sekundy na jedno wywołanie `op` | `60` |
| `MCP_1WRAP_PROFILE` | nazwa profilu | wbudowany `openstack` |
| `MCP_1WRAP_DEBUG` | `1` = diagnostyka na stderr | - |

Gdy ustawisz `OS_CLOUD`, w pliku z hasłami znajdzie się tylko ta jedna chmura, a serwer
dostaje dokładną nazwę z itemu w `OS_CLOUD` i `CLOUD_NAME` (tej drugiej używa `mcp-openstack`).

## Profile (TOML)

`~/.config/mcp-1wrap/profiles/<nazwa>.toml` (`$XDG_CONFIG_HOME` jest respektowany),
wybierany przez `--profile <nazwa>`, `--profile-file <ścieżka>` lub `MCP_1WRAP_PROFILE`.
Profil zawiera nazwy sejfów, tagów i referencje, **nigdy sekrety**.

```toml
vault   = "Private"          # domyślny sejf
timeout = 60                 # sekundy na wywołanie 'op'
unset   = ["OS_TAG"]         # zmienne, których serwer nie dostanie

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
  fikcyjną chmurę `envvars`; launcher usuwa własne pomocnicze zmienne (`OS_TAG`) przed uruchomieniem serwera.
- Pinuj commit wrappera i serwera w configu klienta. Aktualizacja = podmiana hasha po przeglądzie zmian.

## Rozwój

```bash
uv sync
uv run pytest
```

Testy używają atrapy `op` (plik wykonywalny generowany w testach), więc nie wymagają 1Password.
W danych testowych używaj tylko placeholderów (`EXAMPLE-CLOUD`, `*.example`).
