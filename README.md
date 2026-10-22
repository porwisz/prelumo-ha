# Prelumo — one step ahead of the sun

Integracja Home Assistant planująca i sterująca harmonogramem (Time of Use) hybrydowego
falownika z magazynem energii: ładuje baterię w tanich godzinach G13, sprzedaje po RCE gdy
się opłaca, maksymalnie wykorzystuje PV i uwzględnia pompę ciepła oraz ładowanie Tesli.

Prelumo nie rozmawia z Modbusem — korzysta wyłącznie z encji i akcji HA
(domyślnie ESPHome `deye-modbus`, encje `deye_falownik_*`).

## Jak działa

```
co 30 s   odczyt harmonogramu → aktywny slot → tryb pracy (Export First tylko w slocie Sell)
co godz.  prognozy → optymalizator godzinowy na cały horyzont (36 h, DP po SoC)
          → sloty kroczące: sloty 1–5 = kolejne odcinki planu godzinowego (bez ściskania
            całej doby do 6 slotów), slot 6 = bezpieczny ogon do końca doby
          → porównanie z falownikiem → zapis tylko gdy: falownik doszedł do 5. slotu
            (przesunięcie: bieżący slot staje się pierwszym), istotna zmiana zachowania,
            nowy plan tańszy od obecnego o ≥ „min. zysk” (domyślnie 0,50 zł/24 h)
            albo obecny łamie regułę, a nowy nie; maks. 6 zapisów/dobę
raz/dobę  nauka: profil zużycia, model pompy ciepła, wzorzec Tesli, korekta Solcast,
          rzeczywisty koszt wczoraj (tryb cienia), statystyki doby (autarkia, cykle baterii)
```

Sloty kroczące: plan godzinowy liczony jest na całą dobę i falownik dostaje z niego tyle, ile
mieści się w slotach — pierwsze 5 odcinków dokładnie, a szósty slot to „ogon” do końca doby.
Ogon jest wybrany tak, by nie łamał reguł, nawet gdyby nowy harmonogram nigdy nie dotarł
(np. wyczerpany limit zapisów). Gdy falownik dochodzi do 5. slotu, Prelumo zapisuje harmonogram
od nowa od bieżącej godziny — jeden zapis obejmuje więc 4 zmiany slotów w ciągu doby.
Sloty 1–5 ocenia się razem z dalszym planem godzinowym (kolejne przesunięcia go wykonają),
więc ładowanie pod sprzedaż za slotami nie jest usuwane jako „strata”. Ogon dobierany jest
osobno, a regułę, którą łamie, plan pokazuje w `tail_issues` (bez kary w koszcie).
Optymalizator planuje tylko sprzedaże, które falownik wykona: slot sprzedaży oddaje co najmniej
„min. moc sprzedaży” aż do progu, potem dom kupuje z sieci — więc gdy dom pobiera, sprzedaż
musi trwać prawie całą godzinę (np. 0,8 kWh przy 6 kW nie jest możliwe; tolerancja 0,15 kWh
zakupu po progu). Progi sprzedaży w slotach są dopasowane do SoC, który falownik naprawdę będzie
miał na początku sprzedaży (krzywa BMS spowalnia ostatnie %): slot sprzedaje tyle, ile plan.
Ustawienie godzinowe, które mimo to łamie regułę w symulatorze falownika, jest poprawiane:
scalenie z sąsiadem, wyższa podłoga sprzedaży albo zwykły slot — tylko gdy obniża to koszt doby.

Zapisy do falownika (pamięć flash) są oszczędzane:
- koszt i reguły obu harmonogramów — obecnego i nowego — liczone są na tych samych 24 h;
  obecny oceniany jest tak, jak zadziała po przesunięciu (od jego ostatniego slotu dalej
  według planu), więc zysk mierzy tylko to, co naprawdę się zmieni;
- zysk to same pieniądze, złamane reguły ocenia się osobno;
- obecny łamie regułę w ciągu 3 h, a nowy nie → zapis od razu (poza rozłożeniem limitu na dobę);
- reguła złamana dalej → zapis bez progu „min. zysk”, ale w ramach limitu rozłożonego na dobę;
- nowy plan łamie regułę, której obecny nie łamie (i nie łamie mniej reguł) → bez zapisu;
- pierwsza różnica w zachowaniu dopiero za ≥ 12 h → bez zapisu, kolejne przeliczenia ją zobaczą.

| Wejście | Źródło (domyślnie) |
|---|---|
| Ceny zakupu | taryfa G13 z opcji (strefy lato/zima, weekendy i święta = pozaszczyt) |
| Ceny sprzedaży | `sensor.rce_pse_price` + `_tomorrow` (okresy 15 min → godzina) |
| PV | Solcast `detailedHourly` × współczynnik korekty (miesiąc × godzina) |
| Zużycie domu | profil dzień tygodnia × godzina z recordera (bez PC i Tesli) |
| Pompa ciepła | `sensor.sprsun_silnik_moc` — regresja stopniogodzin + profil CWU |
| Tesla | ESP32 BLE: SoC, limit, podłączenie, moc; kalendarz; ręczny cel |
| Awaria sieci | `sensor.deye_falownik_siec_napiecie_l1` < 180 V |
| Ostrzeżenia pogodowe | encja MeteoAlarm / IMGW (opcjonalnie) |
| Zapowiedziane wyłączenia | kalendarz wyłączeń (opcjonalnie) |
| Koszt energii w baterii | liczniki `suma_bateria_ladowanie/_rozladowanie`, `suma_siec_pobor`, `suma_dom`, `suma_pv` |

Moc ładowania Tesli: z czujnika mocy (ESP32) podczas ładowania — zapamiętana na później;
„moc ładowarki” z opcji służy tylko, dopóki nie było pomiaru.

„Tesla ładuje tylko w tanich godzinach” (domyślnie wł.): harmonogram w aucie ładuje tylko w tanich
godzinach G13 wg zegara — przerywa w popołudniowym szczycie (zima 16:00, lato 19:00) i wraca
wieczorem (21:00 / 22:00), jeśli auto jest podłączone. Prognoza rozkłada energię auta tylko na
te godziny (bez reguły weekendów i świąt — harmonogram auta ich nie zna).

Priorytet zapotrzebowania Tesli: ręczny cel > kalendarz (start wydarzenia = wyjazd,
„90%” w tytule = cel) > auto podłączone (SoC → limit) > wyuczony wzorzec.
Bateria domowa nie ładuje Tesli, chyba że termin wyjazdu jest zagrożony albo jest awaria sieci.

### Limity

- **Maks. cena ładowania z sieci** — bateria ładuje się z sieci tylko, gdy cena zakupu ≤ limit
  (domyślnie 0 = najniższa cena z taryfy G13, czyli w praktyce tylko strefa pozaszczytowa).
- **Bateria najpierw zasila dom w drogich godzinach** (domyślnie wł.) — gdy cena zakupu jest
  wyższa niż maks. cena ładowania z sieci, dom zasila bateria aż do min. SoC; Prelumo nie kupuje
  prądu po to, żeby sprzedać baterię później. Sprzedawane jest tylko to, co zostanie.
- **Margines rezerwy na drogie godziny** (domyślnie 30%) — przy „bateria najpierw” sprzedaż jest
  dozwolona tylko ponad prognozowane zużycie domu i pompy ciepła w pozostałych drogich godzinach
  (do najbliższej taniej godziny) plus ten margines. Nie ma „sprzedaj o 19, kup o 20”.
- **Energia z sieci czeka na drogie godziny** — w taniej strefie bateria nie zasila domu energią
  kupioną z sieci (bez cyklu baterii dla taniego prądu). Prelumo śledzi, czy w baterii jest energia
  z sieci (ładowanie w slocie z sieci ustawia, zejście do min. SoC kasuje); energia z PV jest
  używana normalnie. Atrybut `grid_energy_in_battery` w `sensor.prelumo_proposed_plan`.
### Ile kosztuje energia w baterii

`sensor.prelumo_battery_energy_cost` prowadzi średnią ważoną kosztu nabycia: ładowanie z sieci
wchodzi po cenie zakupu danej chwili (podzielonej przez sprawność ładowania), ładowanie z PV po
0 zł, rozładowanie zdejmuje energię po bieżącej średniej. Zejście do min. SoC zeruje licznik —
bateria jest pusta, więc to, co kupione, zostało zużyte.

Źródłem są liczniki energii falownika (`suma_bateria_ladowanie/_rozladowanie`, `suma_siec_pobor`,
`suma_dom`, `suma_pv`), nie chwilowa moc, więc przegapiony odczyt nic nie psuje. Ilość energii jest
raz na godzinę kotwiczona do SoC; przed pierwszym zejściem do min. SoC wartość jest szacunkiem
(atrybut `estimated`).

Atrybut **`break_even_sell_price`** = średnia / sprawność rozładowania + koszt zużycia baterii —
poniżej tej ceny RCE sprzedaż traci pieniądze.

- **Cena wymuszonej sprzedaży** (domyślnie 0 = wył.) — gdy RCE przekracza ten próg, bateria
  sprzedaje mimo rezerwy na drogie godziny; dom dokupuje wtedy z sieci, bo sprzedaż zarabia więcej.
  Dotyczy tylko godziny piku — godziny wcześniejsze nadal trzymają rezerwę. Min. SoC przy
  sprzedaży obowiązuje zawsze.
- **Cena blokady eksportu** (domyślnie 0) — przy RCE ≤ tej wartości Prelumo nigdy nie ustawia
  Export First. Plan i tak nie planuje sprzedaży bez dodatniej ceny (twarda reguła
  `sell_without_price`), ale ten próg jest sprawdzany **na żywo co 30 s**, więc działa też wtedy,
  gdy plan jest stary albo cena się zmieniła. Brak ceny dla danej godziny też blokuje eksport.
  Widoczne jako atrybut `export_blocked` w `sensor.prelumo_active_slot`. Ceny godzinowe RCE są
  zapisywane (3 dni), więc zaraz po restarcie ten sprawdzian nie jest „niewidzący” — bez tego
  eksport byłby blokowany do pierwszego przeliczenia planu.
- **Min. SoC przy sprzedaży** (domyślnie 20%) — bateria nie sprzedaje do sieci poniżej tego poziomu;
  slot Sell nigdy nie dostaje niższego progu.

### Ładowanie do pełna

- **Maks. ładowanie na co dzień** (domyślnie 98%) — ostatnie procenty ładują się długo, więc
  na co dzień plan i sloty ładowania z sieci kończą na tym poziomie.
- **Pełne ładowanie co N dni** (domyślnie 7, 0 = wył.) — gdy termin (N dni od ostatnich 100%)
  wypada w ciągu 24 h, Prelumo planuje jedno ładowanie do 100% (balansowanie BMS) najtańszym dozwolonym sposobem
  (PV, tanie godziny; limit ceny obowiązuje). Monitorowanie: `sensor.prelumo_last_full_charge`
  (data ostatnich 100%, `days_since`, `next_due`, `planned`) i `binary_sensor.prelumo_full_charge_due`.
  Ostatnie 100% Prelumo odczytuje z historii SoC przy starcie i na bieżąco z falownika.

### Ładowanie awaryjne (burza, zapowiedziane wyłączenie)

- **Ostrzeżenia pogodowe** — encja MeteoAlarm/IMGW (`binary_sensor` lub licznik ostrzeżeń
  w `sensor`). Gdy ostrzeżenie jest aktywne, Prelumo doładowuje baterię do **SoC ładowania
  awaryjnego** (domyślnie 100%) **bez limitu ceny ładowania** i bez reguły „bateria najpierw" —
  tu światło w domu jest ważniejsze niż cena. Zmiana stanu ostrzeżenia wymusza przeliczenie
  natychmiast, nie czeka na pełną godzinę.
- **Zapowiedziane wyłączenia** — kalendarz z wydarzeniami wyłączeń. Początek najbliższego
  wydarzenia minus **wyprzedzenie** (domyślnie 2 h) jest terminem, do którego bateria musi być
  naładowana; optymalizator wybiera do tego najtańsze godziny przed terminem.
- To osobna ścieżka niż rutynowe **pełne ładowanie co N dni** (balansowanie BMS) — tam limit ceny
  obowiązuje. Status: `binary_sensor.prelumo_alert_charge` (atrybuty `reason`, `target_soc`,
  `hours_left`, `planned_outage`).

### Strategie
- **Autokonsumpcja** — bateria nigdy nie oddaje do sieci; może ładować się z sieci w strefie
  pozaszczytowej, jeśli to obniża koszt.
- **Maks. handel z siecią** — bateria sprzedaje, gdy RCE to opłaca.
- **Arbitraż z siecią** (osobny przełącznik, domyślnie wył.) — czy energię pobraną z sieci wolno
  potem sprzedać. Gdy wyłączony, po ładowaniu z sieci bateria nie eksportuje, dopóki nie zejdzie
  do min SoC.
- **Min. zysk z arbitrażu** (domyślnie 0,50 zł/kWh) — przy włączonym arbitrażu energia z sieci jest
  sprzedawana tylko, gdy zarobi co najmniej tyle na kWh ponad zakup, straty i zużycie baterii.

## Encje

| Encja | Opis |
|---|---|
| `sensor.prelumo_active_slot` | tryb aktywnego slotu; atrybuty: wszystkie sloty z falownika |
| `sensor.prelumo_next_change` | czas następnej zmiany slotu |
| `sensor.prelumo_proposed_plan` | plan Prelumo: sloty, plan godzinowy, koszt, decyzja zapisu |
| `sensor.prelumo_expected_savings` | oszczędność 24 h vs zwykła autokonsumpcja |
| `sensor.prelumo_shadow_savings` | suma oszczędności z trybu cienia (+ koszt rzeczywisty per dzień) |
| `sensor.prelumo_self_sufficiency` | autarkia 30 dni (%); atrybuty: `yesterday`, `last_7_days`, `by_month` |
| `sensor.prelumo_battery_cycles` | cykle baterii (licznik falownika / pojemność); atrybuty: `last_30_days`, `by_month` |
| `sensor.prelumo_battery_energy_cost` | średni koszt energii w baterii (zł/kWh); atrybuty: `break_even_sell_price`, `stock_kwh`, `grid_share` |
| `sensor.prelumo_ev_energy_needed`, `_load_forecast`, `_pv_forecast_corrected` | prognozy |
| `binary_sensor.prelumo_sell_now`, `_grid_outage`, `_fallback_active`, `_alert_charge` | |
| `select.prelumo_strategy` | autokonsumpcja / maks. handel |
| `switch.prelumo_auto_mode` | zgoda na sterowanie (tryb pracy i zapisy) |
| `switch.prelumo_shadow_mode` | **domyślnie włączony** — próba na sucho: liczy plan i tryb pracy, niczego nie zmienia w falowniku |
| `switch.prelumo_grid_arbitrage` | |

## Gdzie zobaczyć konfigurację i wyniki

- **Panel „Prelumo” w pasku bocznym** — dashboard tworzony przez integrację (strategia Lovelace `custom:prelumo`, aktualizuje się z każdą wersją): harmonogram falownika vs propozycja (z mocami slotów), przebieg godzinowy, decyzje zapisu, prognozy i ceny RCE. Nie da się go edytować — to celowe.
- **Panel „Prelumo” → Edycja** — ręczny harmonogram: start, tryb, SoC i moc każdego slotu (encje `time/number/select.prelumo_slotN_*`), przyciski „Wczytaj z falownika” / „Zapisz do falownika”. Szkic pokazuje falownik, dopóki go nie zmienisz. Przy włączonym „Sterowaniu” Prelumo może go nadpisać przy kolejnym przeliczeniu.
- **Ustawienia → Urządzenia i usługi → Prelumo → Konfiguruj** — parametry baterii, G13, Tesli, plan awaryjny.
- **⋮ → Zmień konfigurację** (reconfigure) — encje źródłowe (RCE, Solcast, pogoda, PC, Tesla, awaria).
- **⋮ → Pobierz diagnostykę** — jeden JSON: konfiguracja, modele, plan godzinowy, decyzje zapisu.
- Atrybuty `sensor.prelumo_proposed_plan` / `sensor.prelumo_active_slot` (`desired_mode`, `mode_action`).

## Usługi

```yaml
action: prelumo.replan          # przelicz teraz (zwraca plan); write: true = zapisz mimo trybu cienia
action: prelumo.read_plan       # bieżący + proponowany plan
action: prelumo.apply_plan      # source: proposed | fallback, albo slots: [6 × {start, soc, mode}]
action: prelumo.set_slot        # slot: 4, soc: 40, mode: sell
```

## Wdrożenie

1. HACS → Integracje → ⋮ → Własne repozytoria → `https://github.com/porwisz/prelumo-ha` (Integracja),
   albo skopiuj `custom_components/prelumo` do `/config/custom_components/`.
2. Dodaj integrację Prelumo, sprawdź encje źródłowe, w opcjach wpisz parametry baterii,
   ceny G13 i **plan awaryjny**.
3. **Wyłącz** `automation.deye_tryb_pracy_wg_flagi_sell_w_harmonogramie` i
   `input_boolean.deye_automatyczny_eksport` — Prelumo przejmuje przełączanie trybu pracy
   (`switch.prelumo_auto_mode`).
4. Przez 1–2 tygodnie zostaw tryb cienia, obserwuj `sensor.prelumo_shadow_savings`
   i `sensor.prelumo_proposed_plan`, potem wyłącz tryb cienia.

## Rozwój

```bash
python3 -m pytest
```

Logika w `custom_components/prelumo/core/` jest czystym Pythonem (bez HA, bez numpy) —
testy w `tests/core/` nie wymagają Home Assistanta.

Etap 2: sterowanie ładowaniem Tesli (BLE: start/stop, amperaż) i przesuwanie pracy pompy ciepła
na nadwyżkę PV / strefę pozaszczytową.

## Licencja

MIT
