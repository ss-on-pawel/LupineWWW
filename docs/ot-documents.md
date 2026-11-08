# Dokument OT

Wspólna funkcja AMS. Numer OT jest niezależnym, opcjonalnym polem
`Asset.ot_document_number` (120 znaków), dostępnym w formularzu i kartotece,
opcjonalnej kolumnie ewidencji, filtrach, API i eksporcie CSV.
Zwykła edycja podlega dotychczasowej kolejce zatwierdzeń i historii zmian.

## Wydruk

W pełnej wersji przycisk **Dokument OT** w kartotece otwiera formularz numeru,
daty przyjęcia, jednostki i uwag. Data domyślnie pochodzi z kartoteki; brak daty
wymaga jej podania. Numer oraz data zatwierdzone przy generowaniu są zapamiętywane
w kartotece. Data dokumentu jest polem `commissioning_date`, widocznym w ewidencji
jako „Przyjęcie do użycia”. Nie podstawiamy dzisiejszej daty za nieznaną historyczną.

Numer wpisuje użytkownik według numeracji jednostki; nie generujemy go z liczby
załączników. Ten sam numer może dotyczyć kilku kartotek. Jeden PDF dotyczy jednej
kartoteki. Zmianę numeru przy generowaniu mogą zapisać tylko użytkownicy, których
zmiany kartoteki nie wymagają akceptacji. Pozostali mogą drukować zatwierdzony
numer i datę, a ich zmianę zgłaszają normalną edycją kartoteki.
Formularz podpowiada przykład `OT/001/2026`, bez narzucania go historycznym numerom.

Generator OT współdzieli z LT czcionki, nagłówek, obramowania, podpisy, stopkę
i mechanizm stronicowania. Treść jest dostosowana do przyjęcia. Drukuje ilość
ewidencyjną oraz zapisaną wartość zakupu, bez przeliczania jej przez ilość.
Zapisuje datę przyjęcia, bez zmiany daty zakupu, statusu ani ilości kartoteki. Można wydrukować historyczne OT
również dla kartoteki archiwalnej.

PDF jest chronionym systemowym załącznikiem typu OT. Numer dokumentu i data
czynności są zapisane także w metadanych załącznika. Każde wygenerowanie tworzy
nowy plik i wpis historii; wcześniejsze PDF pozostają bez zmian.
Formularz sprawdza zmianę numeru lub daty w innej karcie, dopuszczając ponowienie
z tymi samymi danymi. Zapis numeru, daty, metadanych i historii
jest transakcyjny; błąd usuwa wyłącznie nowo utworzony plik.

Pole OT pozostaje częścią danych kartoteki w Full i Lite. Generowanie i załączniki
są dostępne w Full, analogicznie do istniejących dokumentów LT.

## Aktualizacja danych

Migracja `assets.0029_asset_ot_document` dodaje puste pola i typ OT. Nie zmienia
starych numerów, faktur, opisów ani załączników. Starsze oczekujące wnioski edycji
nie tracą zgodności i zachowują OT przy zatwierdzeniu.

Po backupie i zatwierdzonej migracji można przeanalizować numery biblioteki:

```console
python manage.py backfill_ot_documents --external-id-prefix biblioteka-grodzisk:
```

Domyślnie jest to dry run. Polecenie czyta `fields["Dokument OT"]` wyłącznie
z chronionych załączników JSON zatytułowanych „Oryginalna kartoteka klienta”.
Nie zgaduje numerów z opisów. Zachowuje dokładny tekst źródłowy. Konflikt z
istniejącym numerem, sprzeczne źródła albo uszkodzony plik blokują cały zapis.
Powtórzenie jest idempotentne. Zapis po osobnym zatwierdzeniu:

```console
python manage.py backfill_ot_documents --external-id-prefix biblioteka-grodzisk: --apply
```

Polecenia produkcyjne wymagają właściwych ustawień instancji i aktualnego backupu
bazy oraz media. Nie kasują źródłowych JSON ani opisów i nie tworzą historycznych PDF.
Narzędzie nie jest automatycznie wywoływane przez migrację.

## Weryfikacja

`python manage.py test assets accounts` oraz `python manage.py makemigrations --check --dry-run`.
Testy OT obejmują uprawnienia, Lite/CSRF, formularz, ochronę i zachowanie kolejnych
PDF, rollback zapisu, historię, eksport oraz bezpieczne odtworzenie numerów źródłowych.
Porównanie generatora LT na identycznych danych z włączonym ReportLab invariant
powinno dawać identyczne bajty PDF. Próbki OT wymagają wizualnej kontroli.

Znany niezależny problem: główny AssetForm nadal wymaga numeru inwentarzowego.
Nie zmieniono tej zasady. Uprawniony użytkownik może zapisać numer OT z formularza
generowania także dla kartoteki z pustym numerem inwentarzowym.
