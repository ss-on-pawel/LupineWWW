# CLAUDE.md — Lupine AMS

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## REGUŁA #1 — NIGDY BEZ POTWIERDZENIA

Nie tworzę, nie modyfikuję, nie usuwam niczego bez wyraźnego zlecenia właściciela projektu.
Omawiamy ≠ działamy. Czekam na "zrób", "tak", "rób" albo inny wyraźny sygnał.
Dotyczy to plików, kodu, migracji, deploymentu — absolutnie wszystkiego.

---

## O PROJEKCIE

Lupine AMS to aplikacja Django do zarządzania majątkiem (Asset Management System).
Właściciel: Paweł Kosson. Projekt praktycznie ukończony.

**Repozytorium lokalne:** `C:\Users\kosso\LupineWWW`
**Produkcja:** `app.lupine.com.pl`

---

## AKTUALNY STAN

### Gotowe (core)
- Rejestr majątku: tworzenie, archiwizacja, wycofanie, przywrócenie
- Import/eksport CSV/XLSX
- Etykiety i kody kreskowe z sekwencjami
- Załączniki i dokumenty formalne
- Dokumenty LT (likwidacja/wycofanie) z generowaniem PDF
- Alerty serwisowe majątku
- Sesje inwentaryzacyjne: skanowanie, import, ręczne ilości, potwierdzenia ręczne
- Mobilny endpoint skanowania przez token
- Raporty rozbieżności inwentaryzacyjnych
- Kolejka zatwierdzeń zmian majątku (AssetChangeRequest)
- Historia zmian majątku
- Drzewo lokalizacji i ustawienia organizacji
- Niestandardowy model użytkownika, role, flagi zatwierdzania
- Test regresji dla anonimowego przekierowania API assets
- Planowanie amortyzacji środków trwałych (metoda liniowa i jednorazowa)

### Pozostało do zrobienia
- Korekty wartości środka trwałego (zwiększenie/zmniejszenie) — potrzebne m.in. dla historii amortyzacji
- Raporty (inne niż amortyzacja — zakres do ustalenia)
- Optymalizacje wydajności (zapytania, widoki)
- Zabezpieczenia — security hardening
- Dodatkowa funkcjonalność na zamówienie (przyszłość)

### Stan produkcji
- Ostatnia potwierdzona migracja: `[X] 0027_assetdepreciationplan_stored_amounts`
- Ostatni deploy (2026-05-19): commity do `5f3d79f` włącznie (zestawienie odpisów amortyzacyjnych)
- Aplikacja działa poprawnie po restarcie 2026-05-19 21:39 UTC

---

## KOMENDY LOKALNE

```bash
python manage.py runserver                                           # serwer deweloperski
python manage.py test                                                # wszystkie testy
python manage.py test assets                                         # testy jednej aplikacji
python manage.py test assets.tests.NazwaKlasy.nazwa_testu           # pojedynczy test
python manage.py makemigrations --check                              # sprawdź przed deployem
python manage.py shell                                               # Django shell
```

SSH na produkcję (komendy nieinteraktywne):
```bash
ssh pawel@159.69.105.40 "cd /srv/lupine/app && . venv/bin/activate && <komenda>"
```

> Używaj `ssh` (nie pełnej ścieżki `C:\Windows\System32\OpenSSH\ssh.exe` — nie działa w Bash tool).

---

## ARCHITEKTURA

**Stack:**
- Python / Django 5.2.x
- PostgreSQL (produkcja), SQLite (lokalna dev)
- Szablony server-rendered (Django templates)
- nginx + gunicorn + systemd (produkcja)
- ReportLab — generowanie PDF
- openpyxl — import/eksport XLSX
- Bez Dockera

**Struktura repo:**
```
LupineWWW/
  accounts/     profil użytkownika, role, flagi zatwierdzania
  assets/       rejestr, etykiety, dokumenty LT, załączniki, alerty
  config/       ustawienia Django, routing URL, WSGI
  inventory/    sesje inwentaryzacyjne, skany, rozbieżności
  locations/    drzewo lokalizacji, ustawienia organizacji
  templates/    szablony HTML (po polsku)
  users/        niestandardowy model User
```

**Kluczowe modele:**
- `users.User` — niestandardowy model użytkownika
- `accounts.UserProfile` — role i flagi zatwierdzania
- `assets.Asset` — główny rekord majątku (pola: `purchase_value`, `commissioning_date`, `purchase_date`, `category`, `asset_type`)
- `assets.AssetTypeDictionary` — typy majątku, ilości, prefix kodu kreskowego
- `assets.AssetBarcodeSequence` — sekwencje kodów kreskowych
- `assets.AssetChangeRequest` — kolejka zatwierdzeń zmian
- `assets.AssetAttachment` — załączniki i dokumenty formalne
- `assets.AssetServiceAlert` — alerty serwisowe
- `assets.AssetDepreciationPlan` — plan amortyzacji (OneToOne z Asset); pola: `enabled`, `method`, `initial_value`, `residual_value`, `annual_rate_percent`, `depreciation_start_date`, `useful_life_months`, `monthly_depreciation_amount`, `annual_depreciation_amount`, `kst_category`
- `inventory.InventorySession` — sesja inwentaryzacyjna

**Generowanie PDF:** ReportLab, logika w `assets/views.py` (wzorzec: widoki LT). Szablony HTML w `templates/assets/`.
**Eksport XLSX:** openpyxl, logika w `assets/views.py`.
**Obliczanie amortyzacji:** metoda `calculate_depreciation_amounts()` na `AssetDepreciationPlan`; wyniki zapisywane w polach `monthly/annual_depreciation_amount` przy zapisie.

---

## REGUŁY DOMENOWE

**Kody kreskowe i identyfikatory:**
- Prefix kodu wynika z `AssetTypeDictionary`
- Numery inwentaryzacyjne i kody to identyfikatory operacyjne — nie regenerować bez wyraźnego zlecenia
- Import musi zachować dostarczone identyfikatory

**Ilości:**
- Zachowanie ilościowe kontrolowane przez metadane typu majątku
- Inwentaryzacja ilościowa: ilość odczytana + ilość ręczna
- Majątek bez ilości: potwierdzenie obecności / odczytu / ręczne
- Ilość ręczna: nieujemna liczba całkowita

**Dokumenty LT:**
- LT = dokument likwidacji lub wycofania majątku
- Powiązane z zarchiwizowanym lub wycofanym majątkiem
- Generowanie PDF musi być spójne z zaznaczonym majątkiem i metadanymi dokumentu

**Inwentaryzacja:**
- Sesja robi snapshot stanu majątku w danym momencie
- Zamknięcie/zastosowanie sesji może aktualizować stan i ilości majątku
- Raporty rozbieżności: brakujące, nadmiarowe, różnice ilości
- Logika ilościowa musi pozostać oddzielona od logiki potwierdzenia obecności

**Zatwierdzenia:**
- Użytkownicy z flagą zatwierdzania tworzą `AssetChangeRequest` zamiast bezpośrednich zmian
- Role uprzywilejowane mogą pomijać kolejkę zatwierdzeń
- Bulk approve/reject dla wielu wniosków naraz
- Nie omijać reguł zatwierdzania podczas pracy nad funkcjami

**UI i szablony:**
- Etykiety użytkownika po polsku — zachować spójność z istniejącymi szablonami
- Nie przeprojektowywać UI bez wyraźnego zlecenia
- Nie redesignować sąsiednich ekranów przy pracy na konkretnej stronie

---

## DEPLOYMENT — FAKTY PRODUKCYJNE

**Serwer:**
- Provider: Hetzner, CPX31, Ubuntu 24.04
- App path: `/srv/lupine/app`
- Domain: `app.lupine.com.pl`
- SSH: `pawel@159.69.105.40`
- Interaktywna sesja: `ssh pawel@159.69.105.40 -t "cd /srv/lupine/app && source ~/.bashrc && bash -i"`
- Komendy nieinteraktywne: `ssh pawel@159.69.105.40 "cd /srv/lupine/app && . venv/bin/activate && <komenda>"`

**Stack produkcyjny:**
- Python 3.12.3, venv: `/srv/lupine/app/venv/bin/python`
- Gunicorn 26.0.0, 3 sync workers, socket: `/srv/lupine/app/lupine.sock`
- Systemd service: `lupine.service` (User=pawel, Group=www-data, Restart=no)
- nginx: `/etc/nginx/sites-enabled/lupine`, proxy do Unix socket
- PostgreSQL driver: `psycopg==3.3.4`, `psycopg-binary==3.3.4`
- TLS: certbot, `/etc/letsencrypt/live/app.lupine.com.pl/`

**Krytyczne:**
- Produkcja ma lokalny commit `9666e5b Configure production database settings` — nie nadpisywać przez `reset --hard`
- Ustawienia ładowane z `.env` przez `load_dotenv(BASE_DIR / ".env")`
- `sudo nginx -t` (nie `nginx -t` bez sudo — brak dostępu do certbot)
- Nie przechowywać sekretów w repozytorium ani w tym pliku

**Procedura deploy:**
1. Potwierdź scope i zmienione pliki
2. Sprawdź migracje lokalnie (`python manage.py makemigrations --check`)
3. Uruchom testy dla zmienionych modułów
4. Push do GitHub (`git push origin main`)
5. Na VPS: `git pull origin main`
6. Zależności tylko jeśli zmienił się `requirements.txt`: `pip install -r requirements.txt`
7. Migracje: `python manage.py migrate`
8. Collectstatic tylko jeśli zmieniły się static/szablony: `python manage.py collectstatic --noinput`
9. `sudo systemctl restart lupine.service`
10. Sprawdź logi, przetestuj kluczowe ścieżki użytkownika

---

## PROTOKÓŁ SESJI

### START SESJI
1. Czytam ten plik w całości
2. Mówię właścicielowi: *"Stan projektu: [X]. Ostatnia zmiana: [Y]. Co robimy dzisiaj?"*
3. Czekam na potwierdzenie stanu i konkretne zlecenie
4. Jeśli mam wątpliwości — pytam PRZED rozpoczęciem jakiejkolwiek pracy

### PODCZAS SESJI
- Jedno konkretne zadanie na sesję — nie zaczynam kolejnego bez potwierdzenia
- Minimalna zmiana — tylko pliki potrzebne do zadania
- Bez ukrytych refaktorów, bez sprzątania poza zakresem
- Po każdej istotnej decyzji lub zakończonej zmianie → aktualizuję ten plik natychmiast
- Podaję dokładne ścieżki zmienionych plików
- Zgłaszam ryzyka i testy których nie wykonałem

### LISTA STOP — zawsze czekam na wyraźne "tak, rób"
- Cokolwiek dotyka serwera produkcyjnego
- Migracje bazy danych (lokalne i produkcyjne)
- Zmiany uwierzytelniania i uprawnień
- Usuwanie danych lub plików
- Zmiany w `requirements.txt`
- Push do zdalnego repozytorium

### KONIEC SESJI
1. Aktualizuję sekcję "Aktualny stan" w tym pliku
2. Zapisuję podjęte decyzje w sekcji "Decyzje i zmiany"
3. Zapisuję otwarte sprawy i następne kroki

### FRAZA ODZYSKIWANIA
Jeśli sesja padnie, właściciel mówi: **"Czytaj CLAUDE.md i powiedz gdzie jesteśmy."**
Czytam plik, streszczam stan w 3 zdaniach, kontynuujemy od miejsca w którym skończyliśmy.

### ZAPIS NA BIEŻĄCO
Nie czekam na koniec sesji. Po każdej ważnej decyzji lub zmianie kierunku — aktualizuję ten plik od razu. Długa sesja może paść — plik musi zawierać aktualny stan w każdej chwili.

---

## DECYZJE I ZMIANY

- `2026-05-18` — `381136b` — dodano `@login_required` do `asset_list_api`, deploy potwierdzony smoke testem
- `2026-05-18` — `a18f9a2` — naprawiono duplikaty UNKNOWN_CODE w serwisie inwentaryzacji mobilnej
- `2026-05-18` — produkcja: potwierdzone fakty serwera, nginx, gunicorn, ścieżki, certbot
- `2026-05-19` — `4eb1f74` — test regresji dla anonimowego przekierowania `assets:api-list`
- `2026-05-19` — `1a4093a` — .gitignore: ignorowanie lokalnych mediów, plików .claude, logów
- `2026-05-19` — `d1f6446`–`e590e69` — amortyzacja środków trwałych (plan, podgląd, zapis), migracje 0026+0027, wdrożone na produkcji
- Do zrobienia: race condition mobilnego skanowania (session locking lub DB unique constraint po czyszczeniu duplikatów na produkcji)
- `2026-05-19` — zestawienie odpisów amortyzacyjnych: `assets/report_utils.py` (nowy), `assets/documents.py` (PDF landscape), `assets/views.py` + `assets/urls.py` + `templates/assets/depreciation_report.html` (nowy), nawigacja — 0 migracji, 510 testów zielonych
