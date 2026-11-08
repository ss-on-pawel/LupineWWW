# Wspólna przestrzeń kodów środków i lokalizacji

Każda baza klienta ma własny rejestr `ScanCode`. Ten sam kod nie może zostać
przypisany innemu środkowi lub lokalizacji. Kontrola obejmuje archiwum oraz
nieaktywne lokalizacje. Puste kody środków są dozwolone. Lokalizacja może mieć
ręczny kod do 120 znaków; przy pustym polu nowej lokalizacji zachowano generator
LOC-id, z pomijaniem zajętych oznaczeń.

Formularze pokazują błędy walidacji. Wyzwalacze PostgreSQL/SQLite i unikalny klucz
rejestru zabezpieczają również importy, bulk_create, QuerySet.update i równoczesne
zapisy do dwóch różnych tabel. Niezmieniony własny kod pozostaje dozwolony,
w tym historyczny kod środka równy numerowi inwentarzowemu.

Migracje assets.0032–0033 i locations.0003 tworzą rejestr z dotychczasowych kodów
i instalują zabezpieczenia bez ich renumeracji. Inventory.0009 rozszerza zapis
kodu bieżącej lokalizacji do 120 znaków. Dawny kod pozostaje zajęty przez ten sam
rekord po zmianie oznaczenia, żeby nie zmienił znaczenia starych etykiet i kolejek
offline. Archiwizacja nie zwalnia kodu. Fizyczne usunięcie rekordu usuwa jego
rejestracje; nie wykonujemy żadnych takich usunięć podczas aktualizacji.

Historyczne kolizje są zachowywane i raportowane przez:

```console
python manage.py audit_scan_codes
```

Taki kod nie przełącza lokalizacji ani nie wskazuje automatycznie środka.
Terminal zachowuje raw scan z wynikiem error; telefon zachowuje odczyt jako
nieznany kod z komunikatem do wyjaśnienia. Kontrola przed wdrożeniem na pięciu
aktywnych bazach wykazała zero kolizji.

## Rozpoznawanie

Brak wymaganego prefiksu. Lokalizacje rozpoznawane są po dokładnym kodzie,
z zachowaniem zer początkowych, wielkości liter i spacji wewnętrznych. Skaner
usuwa jedynie otaczające białe znaki transportowe. Nie mapujemy numeru
inwentarzowego na kod kreskowy.

Terminal pobiera aktywne lokalizacje dostępne użytkownikowi, zapisuje zamknięty
słownik w IndexedDB i rozpoznaje kody lokalnie. Słownik nie wymaga wczytania
całej bazy środków. Telefon z publicznym tokenem dostaje lokalizacje z zakresu
sesji. Serwer powtarza walidację aktywności i zakresu; próba użycia lokalizacji
spoza dostępu lub nieaktualnego kodu kontekstu nie przypisuje rzeczy do złego
pomieszczenia. Kod spoza słownika nadal jest zapisywany; serwer rozpoznaje rzecz
lub pozostawia go jako nieznany.

Backend, telefon, terminal i import TXT używają tej samej logiki lokalizacji.
Liczniki rozpoznają rzeczywiste kody, a nie prefiks LOC-. Kod środka zaczynający
się od LOC- pozostaje kodem środka, jeśli nie jest kodem lokalizacji.
Nie przeliczamy masowo starych wyników inwentaryzacji przy aktualizacji.

Nowy shell terminala: v5, klient client-v4.js; stare aliasy zachowane. Po wdrożeniu
otworzyć terminal online i poczekać na gotowość offline. Nie czyścić IndexedDB
ani danych przeglądarki. Kolejka i potwierdzenia zachowane.

## Weryfikacja

Testy wszystkich modułów majątku/lokalizacji/inwentaryzacji/kont, dedykowane
testy walidacji i zapisu, test przeglądarkowy `tests/browser/terminal_offline.cjs`
(w tym dowolne kody, zera, polskie litery i nazwy __proto__/constructor), a przed
wdrożeniem test rzeczywistej równoległości na PostgreSQL w izolowanym schemacie.
