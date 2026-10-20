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

## Zakładka Woda

Pokazuje zużycie i koszty osobno dla licznika głównego (dom) i podlicznika (ogród), dla ostatnich
12 miesięcy albo wybranego roku, porównanie z tymi samymi miesiącami rok wcześniej oraz zmianę
ceny za m³. Podział bierze się z pozycji faktury: tytuły zawierające „gospodarstw" to dom,
„pozostali odbiorcy" to ogród (`METER_RULES` w `app/water_mail.py`). Faktury bez rozpoznanych
pozycji są liczone w całości jako „bez podziału".

## Zakładka Gaz

Działa tak jak „Woda”, ale dla jednego licznika: zużycie w kWh i koszty dla ostatnich 12 miesięcy albo
wybranego roku, porównanie z tymi samymi miesiącami rok wcześniej, koszt na kWh na kolejnych fakturach
oraz wykres kosztów miesiąc po miesiącu z nałożonymi latami (sezonowość). Dane pochodzą z importu eBOK
(`python -m app.pgnig --sync`) lub z wgranych PDF-ów.

Zakładka „Gaz” pokazuje też **stan konta u dostawcy** (nadpłata lub zaległość z ostatniej faktury i na ile
miesięcy wystarczy), **z czego składa się rachunek** (abonament, paliwo, dystrybucja stała i zmienna),
**ceny jednostkowe w czasie** oraz **prognozę roczną** przy obecnych cenach. Te dane pochodzą z PDF-ów
faktur: `python -m app.pgnig --sync` pobiera brakujące PDF-y i uzupełnia pozycje oraz stan konta także
w już zaimportowanych fakturach, a `--reparse` odczytuje ponownie zapisane PDF-y (po zmianie parsera).

## Prąd: Enea

Enea nie wysyła faktur mailem, a nowy eBOK nie ma już prostego formularza logowania (strona jest ładowana
skryptem, a logowanie może wymagać kodu SMS), więc faktur nie pobiera się automatycznie. Zamiast tego:

1. **Powiadomienie mailowe** o nowej fakturze zakłada w aplikacji rekord (numer i data). Włączasz je przez
   `ENEA_SENDER=powiadomienia@ebok.enea.pl` w `.env` (korzysta z `IMAP_*`; obsługuje je usługa `water-sync`).
2. Rekord czeka na PDF (informacja w zakładce „Wgraj PDF”). Po wgraniu PDF dopasowuje się po numerze,
   uzupełnia kwotę, okres i datę, a duplikatów nie tworzy.
3. Faktury Enei (rozliczeniowe, z energią i dystrybucją) są odczytywane w całości: kwota, okres, zużycie
   w kWh, **wynik rozliczenia** (nadpłata lub do zapłaty, czyli „skarbonka” u Enei) i pozycje kosztów
   (energia czynna, opłata stała sieciowa, mocowa, zmienna, jakościowa, OZE, kogeneracyjna, abonamentowa).
   Zakładka „Prąd” pokazuje to samo co „Gaz”: stan konta, zużycie i koszty, rok do roku, z czego składa się
   rachunek, ceny jednostkowe w czasie i prognozę roczną przy obecnych cenach.

### Pobranie wielu PDF-ów naraz (np. historia)

Zaloguj się do eBOK Enei w przeglądarce (z kodem SMS), wejdź na stronę faktur i rozwiń pełną listę. Otwórz
konsolę przeglądarki (F12, zakładka Console; Chrome wymaga wpisania `allow pasting`), wklej poniższy
skrypt i naciśnij Enter. Działa wyłącznie w Twojej przeglądarce, na Twojej sesji, i tylko zapisuje pliki
na dysk. Potem przeciągnij je do zakładki „Wgraj PDF”.

```js
(async () => {
  const links = [...document.querySelectorAll('[data-action="download-document-api"]')];
  console.log('Znaleziono dokumentów:', links.length);
  for (const el of links) {
    const name = (el.textContent || el.dataset.documentId || 'faktura').trim().replace(/[^A-Za-z0-9_-]+/g, '_');
    try {
      const r = await fetch(el.dataset.url, {credentials: 'include'});
      const type = r.headers.get('content-type') || '';
      const blob = await r.blob();
      if (!type.includes('pdf')) { console.warn(name, 'to nie PDF:', r.status, type, (await blob.text()).slice(0, 200)); continue; }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = name + '.pdf';
      document.body.appendChild(a); a.click(); a.remove();
      await new Promise(res => setTimeout(res, 700));
    } catch (e) { console.warn(name, e); }
  }
  console.log('Gotowe');
})();
```

Skrypt jest napisany na podstawie elementów strony faktur (`data-action="download-document-api"`), ale nie
był testowany na prawdziwej stronie. Jeśli coś nie zadziała, komunikat w konsoli powie, co zwrócił serwer.

## Paliwo (ORLEN Pay)

Po każdym tankowaniu przez aplikację ORLEN Pay przychodzi mail z e-fakturą i załączonym PDF-em. Konektor
(włączany przez `FUEL_SENDER=orlenpay@orlen.pl` w `.env`, obsługuje go usługa `water-sync`) zakłada fakturę
(numer, data, stacja z godziną, kwota), zapisuje PDF i odczytuje z niego litry oraz rodzaj paliwa (np. gaz LPG).
Faktury za paliwo można też wgrać w zakładce „Wgraj PDF” (wystawcą bywa dealer stacji, np. spółdzielnia
kółek rolniczych, a dostawca jest rozpoznawany po „ORLEN Pay” i „stacja paliw”) albo dodać ręcznie.
Zakładka „Paliwo” pokazuje koszt, liczbę tankowań, litry, średnią cenę za litr, rok do roku, podział wg
rodzaju paliwa i listę tankowań. Miesiąc bez tankowania liczy się jako zero, a dane są od pierwszej faktury.
Maile mogą leżeć w etykiecie Gmaila (np. „faktury”) zamiast w skrzynce odbiorczej: wtedy ustaw w `.env`
`FUEL_FOLDER=faktury` (albo kilka folderów po przecinku, `INBOX,faktury`; to samo dla Enei: `ENEA_FOLDER`). Etykieta
jest dla IMAP folderem o tej samej nazwie. Ten sam mail w kilku folderach jest liczony raz.

Gdy faktury za paliwo się nie pojawiają, sprawdź, co konektor widzi w skrzynce (niczego nie zapisuje):

    docker compose run --rm water-sync python -m app.fuel_mail --check

Dla każdego maila pokaże datę, temat, liczbę załączników PDF, to, co odczytano z treści i z PDF-a, oraz czy faktura
jest już w bazie. Jeśli `FUEL_SENDER` dopisano do `.env` po uruchomieniu usługi, odtwórz kontener, bo zmienne są
czytane tylko przy jego tworzeniu: `docker compose up -d --force-recreate water-sync`. Dane można brać z samej
treści maila albo z samego PDF-a, więc zmiana wyglądu maila przez ORLEN nie blokuje importu, o ile jest załącznik.
**Okres statystyk** wybierasz w zakładce „Paliwo”: ostatni miesiąc, 2, 3, 6, 12 miesięcy, ostatnie 30 dni, rok albo „od
wybranej daty”. Wybór jest zapamiętywany i określa też średnią paliwa w „Statystykach” oraz „Subskrypcjach”. Średnia
miesięczna to koszt ÷ liczba dni okna × 30,44, a okno jest skracane do daty pierwszego zakupu. **Ręczne tankowanie**
(płatność przy kasie, bez faktury): data, litry i cena za litr (kwota = litry × cena, albo wpisz kwotę z paragonu).
Paliwo jest w statystykach osobno: „Odkładaj miesięcznie” dotyczy tylko mediów (prąd, gaz, woda).

## Karta „Miesiąc”

Wszystkie koszty miesiąca w jednym miejscu, z zaznaczaniem, co już zapłacono. Pozycje składają się same z: faktur
za prąd, gaz i wodę wystawionych w tym miesiącu (a gdy faktury jeszcze nie ma: **szacunku** ze średniego kosztu,
który zastąpi faktura po pobraniu), subskrypcji, kosztów okresowych przypadających w tym miesiącu (po dacie płatności)
oraz własnych pozycji (np. czynsz, rata). Zaznaczasz „Zapłacone” i zapisujesz; zaznaczenia są zapamiętywane per pozycja
i miesiąc. Na górze są sumy (razem, zapłacone, zostało) i pasek postępu. **Zaległe** to niezapłacone pozycje z poprzednich
miesięcy, liczone od miesiąca rozpoczęcia śledzenia (domyślnie miesiąc pierwszego otwarcia karty, zmienisz w „Od kiedy
liczyć zaległości”), bez szacunków i bez faktur, po których u dostawcy została nadpłata. Subskrypcje i koszty
okresowe pojawiają się od miesiąca ich dodania.

## Koszty roczne i okresowe

Zakładka „Koszty roczne”: OC, przegląd, ubezpieczenia, podatki, opony itp. (co kwartał, pół roku, rok, 2 lub 3 lata).
Dla każdego kosztu podajesz kwotę, częstotliwość, najbliższy termin i opcjonalnie, ile już odłożono. Program liczy:
**ratę miesięczną** (kwota ÷ długość cyklu, to jest ile odkładać na stałe), **ile powinno być odłożone dziś** (przy równym
odkładaniu od poprzedniej płatności), różnicę względem wpisanego oraz **ile odkładać do terminu**. Po płatności zmień termin
na kolejny i wyzeruj „Odłożono”. Raty wchodzą do sumy „Stałe wydatki” w „Statystykach” i „Subskrypcjach”.

## Subskrypcje

Zakładka „Subskrypcje” to lista stałych opłat (np. Spotify, HBO, abonament telefoniczny): nazwa, kwota, cykl
(miesięcznie, kwartalnie, rocznie), opcjonalna data jednej z płatności i notatka. Płatności roczne i kwartalne
są przeliczane na miesiąc. Subskrypcję można wstrzymać (odznaczenie „Aktywna”) bez usuwania. Zakładka pokazuje
sumy miesięczne i roczne, **stałe wydatki razem** (subskrypcje + media, opcjonalnie paliwo) oraz najbliższe
płatności w ciągu 30 dni (liczone od podanej daty co cykl). W „Statystykach” są dwie dodatkowe metryki:
subskrypcje na miesiąc i stałe wydatki (media + subskrypcje).

## Przeliczanie faktur z zapisanych PDF-ów

Przycisk „Przelicz faktury z zapisanych PDF-ów” (zakładka „Wgraj PDF”) ponownie odczytuje wszystkie zapisane
PDF-y: odtwarza pozycje kosztów, stan konta, kwoty i okresy. Użyj go po aktualizacji programu albo gdy faktury
wgrano starszą wersją. Wynik pokazuje dla każdej faktury liczbę pozycji i ostrzeżenia (brak pozycji, suma
pozycji różna od kwoty). Pozycje kosztów są zapisywane tylko wtedy, gdy ich suma zgadza się z kwotą faktury (z tolerancją na zaokrąglenia);
niespójne pozycje, np. z PDF-a o innym układzie tabeli, są odrzucane, żeby nie zaniżały struktury kosztów i cen.
Dla faktur Enei, których tabeli nie da się odczytać, program próbuje podziału zbiorczego (energia i dystrybucja
razem) z sum sekcji „Ogółem wartość - sprzedaż energii” i „... usługa dystrybucji”, ale tylko gdy po doliczeniu VAT
dokładnie zgadzają się z kwotą faktury. Faktury bez pozycji widać w „Kontroli odczytu pozycji z faktur”.
Przeliczenie można ograniczyć do jednego medium i obserwować na pasku postępu, a z terminala (bez limitów
przeglądarki) uruchomić je poleceniem `docker compose run --rm water-sync python -m app.invoices --reparse prad`
(lub `gaz`, `woda`, `paliwo`, bez argumentu: wszystkie). W zakładkach „Gaz” i „Prąd” jest też „Kontrola odczytu pozycji z faktur”, która
wskazuje faktury odczytane niepełnie.

## Faktury PDF

Zakładka „Faktury PDF”: wybierasz medium, rok i fakturę, klikasz „Pobierz PDF”. Faktury, które nie
mają pliku (np. wpisane ręcznie), możesz uzupełnić, dołączając PDF w tej samej zakładce.
Faktury zawierają dane osobowe i numer konta, więc ustaw `APP_PASSWORD` w `.env` (puste = bez hasła)
albo trzymaj panel wyłącznie w sieci domowej.

## Wgraj PDF

Zakładka „Wgraj PDF”: przeciągasz pobrane z eBOK pliki (Enea, PGNiG, wodociągi), program rozpoznaje
dostawcę, odczytuje dane i zapisuje fakturę razem z plikiem. Wodociągi mają pełny parser (z podziałem
dom/ogród). Gaz i prąd mają odczyt ogólny, dopasowywany do prawdziwych faktur. Duplikaty są pomijane.

## Gaz: PGNiG / myORLEN eBOK

Konektor loguje się do `ebok.myorlen.pl` (nieoficjalne API tego samego serwisu co przeglądarka), pobiera
listę faktur (numer, data, kwota, okres, zużycie w kWh) i PDF-y. Noty (numery zaczynające się od `N`) są
pomijane (`PGNIG_SKIP_PREFIXES`). W `.env`: `PGNIG_LOGIN` i `PGNIG_PASSWORD`. Kolejność uruchamiania:

    docker compose run --rm water-sync python -m app.pgnig --probe             # logowanie i struktura odpowiedzi
    docker compose run --rm water-sync python -m app.pgnig --sync --dry-run    # co zostałoby zaimportowane
    docker compose run --rm water-sync python -m app.pgnig --sync              # import faktur i PDF-ów

Automatyczne pobieranie co dobę: dopisz `COMPOSE_PROFILES=pgnig` do `.env` i uruchom `./deploy.sh`.

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
