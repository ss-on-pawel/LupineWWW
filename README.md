# Projekt zarzadzania majatkiem

Szkielet aplikacji Django przygotowany pod dalszy rozwoj systemu biznesowego.

## Uruchomienie

1. Zainstaluj zaleznosci:
   `py -m pip install -r requirements.txt`
2. Wygeneruj migracje:
   `py manage.py makemigrations`
3. Zastosuj migracje:
   `py manage.py migrate`
4. Utworz konto administratora:
   `py manage.py createsuperuser`
5. Uruchom serwer:
   `py manage.py runserver`
