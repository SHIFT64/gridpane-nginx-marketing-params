# GridPane FastCGI cache: ignorowanie parametrów marketingowych

Robi to samo co „Ignored parameters” w WP Rocket, ale na poziomie nginx w GridPane:

```
example.com/about-us/?gclid=something
example.com/about-us/?gclid=somethingelse&utm_source=fb
// obie wersje dostają plik cache z:
example.com/about-us/
```

- **Bez redirectu.** Parametry zostają w URL-u w przeglądarce, więc GA4, Google Ads (gclid/gbraid/wbraid), Meta Pixel (fbclid → `_fbc`), Microsoft Ads itd. działają normalnie. Tym różni się to od [artykułu z KB GridPane](https://gridpane.com/kb/remove-specific-query-strings-to-load-the-cached-version-of-a-page/), który robi 301 bez parametrów i psuje atrybucję.
- **Wspólny wpis w cache.** Request z samymi parametrami marketingowymi trafia do wpisu czystego URL-a. Dostaje HIT i nagłówek `X-Grid-Cache-Mkt: ignored`.
- **Każdy inny parametr działa jak w stockowym GridPane.** `/?s=foo&gclid=1` czy `/x/?page=2&utm_source=x` → BYPASS, a PHP dostaje oryginalny URL.
- **Bez zatruwania cache.** Gdy odpowiedź idzie do cache, PHP widzi czysty URL. Żaden `gclid=...` pierwszego odwiedzającego nie trafia w linki, paginację czy ukryte pola w zapisanym HTML.
- **Redirecty WordPressa zachowują parametry.** Dotyczy to braku końcowego `/`, www → apex, canonicala i zgadywania URL-a przy 404. nginx dokleja oryginalne parametry marketingowe do `Location`, ale tylko dla tego samego hosta.
- **Purge bez zmian.** Klucz czystego URL-a jest bajt w bajt taki jak w stockowym GridPane, więc Nginx Helper (`/purge/<path>`) czyści też warianty z gclid/utm.
- **Czysty nginx** (`map` + PCRE): bez Lua i njs, bez edycji plików zarządzanych przez GridPane.

## Instalacja

Na serwerze GridPane, jako root:

```bash
git clone git@github.com:kayasparrow/gridpane-marketing-params.git /root/gp-marketing-params
```

```bash
cd /root/gp-marketing-params && ./install.sh twojastrona.pl
```

Co robi skrypt:

1. **Raz na serwer** instaluje silnik i listę w `/etc/nginx/marketing-params/` oraz jednolinijkowy stub w `/etc/nginx/conf.d/`. Istniejąca lista nigdy nie jest nadpisywana.
2. **Dla każdej strony** dokłada jeden jednolinijkowy przełącznik w `/var/www/<site>/nginx/`.
3. Uruchamia `nginx -t`. Jeśli przejdzie, robi reload. Jeśli nie, przywraca poprzednie pliki.

```bash
./install.sh strona1.pl strona2.pl         # kilka stron
./install.sh --status                      # co jest zainstalowane / włączone / podejrzane
./install.sh --disable twojastrona.pl      # wyłączenie dla strony
./install.sh --uninstall                   # usunięcie wszystkiego (lista → /root/marketing-params.list.bak)
./install.sh                               # aktualizacja silnika po git pull (lista zostaje)
MAX_PARAMS=32 MAX_LEN=4096 ./install.sh    # inne limity (zapamiętane w limits.env)
```

Skrypt pomija strony bez FastCGI cache (Redis albo brak cache). Pomija też strony ze zmienionym kluczem cache, np. z GeoIP albo natywną funkcją Lua GridPane.

## Testy

```bash
./test.sh
```

**Piaskownica.** Uruchamia tymczasowy nginx na losowym porcie `127.0.0.1`. Używa binarki i **prawdziwego szablonu GridPane z tego serwera** (`/etc/nginx/common/wpfc.conf`), plików z repo oraz atrapy PHP (`tests/fake_php.py`). Wykonuje 121 sprawdzeń, a na końcu sprząta po sobie. Sprawdza:
- współdzielenie cache i brak zatruwania,
- BYPASS przy innych parametrach,
- limity,
- pierwszeństwo POST, cookies, wykluczonych URI, własnych reguł skip i własnych rewrite'ów,
- redirecty, w tym do obcych hostów i adresy względne,
- purge,
- stronę bez przełącznika,
- `nginx -t` po klonie na serwer bez silnika, bez `params.list` i na stronie bez cache,
- czysty error log.

**Nie dotyka produkcyjnego nginx ani stron.** Warto go uruchomić po każdej aktualizacji GridPane, bo zmianę ich szablonu test od razu wyłapie.

```bash
./test.sh --site twojastrona.pl
```

To samo, ale na wyrenderowanym szablonie konkretnej strony.

```bash
./test.sh live twojastrona.pl /jakas-strona/
```

**Test na żywej stronie po instalacji.** Wysyła tylko GET-y do lokalnego nginx z pominięciem Cloudflare i niczego nie czyści. Sprawdza:
- HIT z gclid,
- wspólny wpis z czystym URL-em,
- BYPASS przy innym parametrze,
- zachowane parametry przy redirectach (bez końcowego `/` i www → apex).

Podaj stronę z końcowym `/`, wtedy wykona się też test redirectu.

Kody wyjścia: `0` = wszystko PASS, `1` = jest FAIL, `2` = błąd przygotowania (np. GridPane zmienił szablon).

## Lista parametrów

Lista jest w pliku `/etc/nginx/marketing-params/params.list`, jedna linia na parametr:

```nginx
gclid 1;        # dokładna nazwa (wielkość liter bez znaczenia)
~*^utm_ 1;      # regex na nazwie: wszystkie utm_* (~* = bez wielkości liter)
```

Po edycji:

```bash
nginx -t && systemctl reload nginx
```

Lista jest jedna na serwer i działa na każdej stronie z włączonym przełącznikiem.

**Złota zasada:** na liście mogą być tylko parametry, które czyta **JavaScript**, a nie PHP. Na stronach z cache PHP ich nie zobaczy, tak samo jak przy każdym HIT.
- Domyślnie wyłączone (opcjonalne, z opisem w pliku): `mc_cid`/`mc_eid` (Mailchimp for WooCommerce ustawia z nich cookie w PHP) oraz `ck_subscriber_id` (Kit).
- Nigdy nie dodawaj: `ref`, `s`, `p`, `page_id`, `lang`, `add-to-cart`, `cn-reloaded`, `age-verified`.

## Jak to działa

```
request /about-us/?gclid=1&utm_source=x
  │
  ├─ GridPane wpfc.conf (server):  query string ≠ ""  → skip_cache=1, skip_reason="-query_string"
  │                                 + Twoje reguły skip (dopisują do skip_reason)
  │
  ├─ location / → try_files … /index.php?$args   (oraz ewentualne Twoje rewrite'y)
  │
  └─ location ~ \.php$  →  marketing-params-php-context.conf  (TU zapada decyzja)
        un-skip tylko gdy:  skip_reason == "-query_string" (jedyny powód)
                            GET/HEAD
                            $args == oryginalne query (nic go nie przepisało)
                            ≤ 2048 B, ≤ 24 parametry, WSZYSTKIE z listy
        → skip_cache=0, klucz …$host/about-us/  (= klucz czystego URL-a)
        → PHP: REQUEST_URI=/about-us/, QUERY_STRING=""
        → 3xx Location (ten sam host) + "?gclid=1&utm_source=x"
```

| Plik | Kontekst | Rola |
|---|---|---|
| `/etc/nginx/marketing-params/params.list` | include w `map` | **lista, którą edytujesz** |
| `/etc/nginx/marketing-params/engine.conf` | http | łańcuch `map`, generowany przez `build-engine.py` |
| `/etc/nginx/marketing-params/php-context.conf` | location php | decyzja, klucz, czysty URL dla PHP, poprawka `Location` |
| `/etc/nginx/marketing-params/limits.env` | – | zapamiętane limity |
| `/etc/nginx/conf.d/marketing-params.conf` | http | stub: `include …/engine[.]conf;` |
| `/var/www/<site>/nginx/marketing-params-php-context.conf` | location php | **przełącznik strony**: `include …/php-context[.]conf;` |

### Odporność

- **Pliki GridPane zostają nietknięte.** Pliki o własnych nazwach przetrwają nocny sync i `gp update`.
- **Klon na serwer bez silnika.** GridPane kopiuje tylko `/var/www/<site>/nginx/`, a include działa przez glob na dokładną nazwę. Na serwerze bez silnika nic się więc nie dzieje, a `nginx -t` przechodzi.
- **Brak zależności od zmiennych GridPane.** Silnik się do nich nie odwołuje, a przełącznik sam je deklaruje. Zmiana typu cache strony w panelu nie wywali `nginx -t`.
- **Własne reguły wygrywają.** Reguła skip, która dopisuje do `$skip_reason` (jak w przykładach z KB GridPane), wyłączy cache także przy gclid. Rewrite zmieniający `$args` też powoduje stockowe zachowanie.
- **Zabłąkane kopie są ignorowane.** Pliki typu `engine.old.conf` czy `php-context.orig.conf` nie są wczytywane, a brak `params.list` znaczy po prostu „nic nie jest marketingowe”.
- **Długie query niczego nie kosztują.** Powyżej 2048 bajtów (albo przy ścieżce dłuższej niż 8 KB) łańcuch się nie uruchamia. Łańcuch kończy się też na pierwszym parametrze spoza listy.

## Ograniczenia i zachowania warte wiedzy

- **Reguła skip bez powodu.** Reguła, która ustawia `$skip_cache 1` **bez** dopisania czegoś do `$skip_reason`, zostanie nadpisana przy requestach z samymi parametrami marketingowymi. Zawsze dopisuj powód: `set $skip_reason "${skip_reason}-moj_powod";`.
- **Nadpisany klucz cache.** Przełącznik nadpisuje `fastcgi_cache_key` dla **każdego** requestu strony stockową formułą `$scheme$request_method$host$request_uri`. Dlatego `install.sh` odmawia włączenia stron z inną formułą klucza.
- **Redirecty do innego hosta.** Parametry wracają tylko do `Location` na tym samym hoście (i jego bliźniaku www/apex) albo na ścieżkę `/…`. Przy redirectach na inną domenę (alias → domena główna, S3, bramki płatności) parametry nie są doklejane. Jeśli cel redirectu ma już własne `utm_*`, parametry się zdublują.
- **Limity.** Domyślnie 24 parametry (puste `&&` się nie liczą), 2048 bajtów query i 8 KB ścieżki. Powyżej tego działa stockowy BYPASS. Każdy parametr limitu to 4 zmienne nginx, więc nie podnoś limitu bez potrzeby (GridPane ma `variables_hash_max_size 2048`).
- **Nazwy surowe.** Nazwy porównywane są w postaci nieodkodowanej, więc `utm%5Fsource` = BYPASS (bezpiecznie).
- **Klon i staging na tym samym serwerze.** GridPane kopiuje przełącznik, więc kopia ma funkcję włączoną od razu. Push stagingu na produkcję włączy ją ponownie, jeśli produkcja miała ją wyłączoną. `./install.sh --status` pokazuje wszystkie strony z przełącznikiem.
- **`Set-Cookie` w cache.** GridPane (stock) cache'uje odpowiedzi razem z `Set-Cookie`. PHP nie widzi wartości marketingowych, więc żadne cookie nie powstaje z gclid ani utm.
- **Natywna funkcja GridPane.** GridPane ma nieudokumentowaną funkcję Lua (`gp stack nginx -lua query-param-cache`). Normalizuje tylko klucz, więc PHP widzi parametry i HTML może zostać zatruty. Nie łącz jej z tym rozwiązaniem na jednej stronie.
- **Purge musi trafić do lokalnego nginx** z tym samym `$scheme` i `$host`. Jeśli domena strony prowadzi przez Cloudflare i nie ma wpisu `127.0.0.1` w `/etc/hosts`, Nginx Helper dostaje 403 od CF i purge nie działa.

## Cloudflare

**Uwaga na reguły CF, które wycinają parametry.** Jeśli w strefie działa Transform Rule usuwająca np. `fbclid`, serwer nigdy go nie zobaczy. Przy mieszanym query (`/?fbclid=1&inny=1`) do serwera trafia wtedy `/?&inny=1`, a WordPress robi canonical 301 do `/?inny=1`. Przeglądarka traci fbclid. Przy tym rozwiązaniu takie reguły w CF są niepotrzebne, więc je wyłącz.

**Alternatywa po stronie CF.** Transform Rule z `remove_query_args()` działa na każdym planie. Ma jednak wady:
- tylko dokładne nazwy, bez `utm_*`,
- osobna konfiguracja w każdej strefie,
- origin w ogóle nie widzi parametrów, więc nic nie dołoży ich do redirectów.

Wykluczanie parametrów z klucza cache bez przepisywania URL-a jest dostępne tylko w planie Enterprise.

## Pliki w repo

```
install.sh                      instalacja / włączanie / wyłączanie / status / deinstalacja
test.sh                         testy: piaskownica (domyślnie) albo `live <site> [/path/]`
build-engine.py                 generator engine.conf
server/marketing-params/        → /etc/nginx/marketing-params/  (php-context.conf, params.list)
server/stubs/                   → /etc/nginx/conf.d/marketing-params.conf
site/                           → /var/www/<site>/nginx/marketing-params-php-context.conf
tests/                          sandbox_test.py, live_test.py, fake_php.py (atrapa PHP-FPM)
```
