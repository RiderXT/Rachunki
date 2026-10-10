# Rachunki domowe

Panel (Streamlit + SQLite) do śledzenia rachunków za prąd, gaz i wodę, ze statystykami
i wyliczaniem miesięcznej kwoty do odkładania. Konektor wodociągów pobiera PDF-y z maili.

## Uruchomienie (Proxmox, LXC/VM z Dockerem)

1. Skopiuj folder na maszynę, `cp .env.example .env` i uzupełnij dane skrzynki IMAP
   oraz `WATER_SENDER`. Dla Gmaila/Outlooka potrzebne jest hasło aplikacji.
2. PDF-y na Synology: utwórz udział NFS/SMB, zamontuj go na hoście Proxmoksa i przekaż do
   kontenera LXC jako bind mount (albo zamontuj w VM). Ustaw `PDF_HOST_DIR` w `.env`
   na ścieżkę do tego montowania. Bez tego PDF-y trafią do `./pdf`.
3. `docker compose up -d --build`, panel: `http://<adres>:8501`.

Panel nie ma logowania. Trzymaj go tylko w sieci domowej lub za reverse proxy z autoryzacją.

## Automatyczne wdrażanie z GitHuba

Na hoście Dockera trzymaj repozytorium jako klon (repozytorium jest publiczne, więc `git clone`
i `git pull` nie wymagają logowania):

    git clone https://github.com/RiderXT/Rachunki.git ~/Rachunki
    cd ~/Rachunki/domowe-rachunki
    cp /ścieżka/do/starego/.env .env          # .env i data/ są poza repozytorium (.gitignore)
    docker compose up -d --build

`deploy.sh` sprawdza, czy na GitHubie są nowe commity, i tylko wtedy robi `git pull` oraz
`docker compose up -d --build`. Wpis w cronie (`crontab -e`):

    */5 * * * * /home/USER/Rachunki/domowe-rachunki/deploy.sh >> /home/USER/deploy.log 2>&1

## Strojenie parsera faktur z wodociągów

Wzorce w `app/water_mail.py` są ogólne, bo układ Twojej faktury nie był znany.
Pobierz jedną fakturę i uruchom:

    docker compose run --rm water-sync python -m app.water_mail --dump /pdf/woda/plik.pdf

Zobaczysz tekst wyciągnięty z PDF-a i to, co parser z niego odczytał (kwota, okres,
numer faktury, zużycie). Gdy czegoś brakuje, popraw regexy `RE_*` albo przyślij mi ten tekst
(zanonimizowany), a dopasuję je. Faktury z niepełnymi danymi dostają status `partial` i można
je uzupełnić w zakładce „Lista”.

## Po zmianie parsera

    docker compose run --rm water-sync python -m app.water_mail --reparse       # ponownie sparsuj zapisane PDF-y
    docker compose run --rm water-sync python -m app.water_mail --reset --yes   # usuń faktury z maila i pobierz od nowa

## Link z maila prowadzi do strony pośredniej

Konektor sam wchodzi na stronę z linku, szuka „Pobierz dokument" (zwykły link, `__doPostBack`
w ASP.NET albo przycisk submit) i pobiera PDF. Jeśli nie znajdzie, w logu pojawi się komunikat,
a mail zostanie ponowiony przy następnym przebiegu. Sprawdź wtedy, co jest na stronie:

    docker compose run --rm water-sync python -m app.water_mail --inspect "<link z maila>"

Link zawiera token dostępu do Twojej faktury, więc przed wklejeniem wyniku gdziekolwiek
usuń z niego `AccessPath`.

## Jak liczone jest „ile odkładać”

Każda faktura jest rozkładana równo na dni swojego okresu rozliczeniowego. Dla każdego medium
bierzemy do 12 ostatnich miesięcy w pełni pokrytych fakturami (miesiące z dziurą są pomijane)
i dzielimy sumę przez liczbę tych miesięcy. Tabela pokazuje, ile miesięcy weszło do średniej.

## Dalsze kroki

- Konektor PGNiG: mail z kwotą jako wyzwalacz, uzupełnianie okresu i PDF z eBOK.
- Konektor Enea: scraping portalu albo wpisywanie ręczne.
