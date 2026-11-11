# Prelumo — one step ahead of the sun

Integracja Home Assistant planująca i sterująca harmonogramem (Time of Use) hybrydowego
falownika z magazynem energii: ładuje baterię w tanich godzinach G13, sprzedaje po RCE gdy
się opłaca, maksymalnie wykorzystuje PV i uwzględnia pompę ciepła oraz ładowanie Tesli.

Prelumo nie rozmawia z Modbusem — korzysta wyłącznie z encji i akcji HA
(domyślnie ESPHome `deye-modbus`, encje `deye_falownik_*`).

## Jak działa

```
co 30 s   odczyt harmonogramu → aktywny slot → tryb pracy (Export First tylko w slocie Sell)
co 5 min prognozy → optymalizator godzinowy na cały horyzont (36 h, DP po SoC)
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
- **Rezerwa domu wg pesymistycznej prognozy Solcast** (domyślnie wł.) — gdy Solcast podaje
  `pv_estimate10`, rezerwa na drogie godziny liczy na tę prognozę złego dnia (po korekcie) zamiast
  stałego procentu „zapas na błąd prognozy PV”; bez niej działa stały procent.
- **Nieopublikowane ceny RCE** (domyślnie 70%) — godziny bez ceny (jutro, przed ok. 14:00) liczone są
  jako ten procent mediany ceny z tej samej godziny w ostatnich 3 dniach, więc plan może zostawić
  energię na jutrzejszy poranek. Notatka planu `sell_price_estimated_Nh`. Eksport i tak wymaga
  prawdziwej ceny bieżącej godziny (blokada eksportu). 0 = jak dawniej: brak przychodu.

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
- **Sprzedaż nie większa niż produkcja PV** (domyślnie wył.) — w miesiącu kalendarzowym plan
  oddaje do sieci nie więcej, niż wyprodukowało PV: liczniki falownika (suma PV − suma oddania
  od początku miesiąca) plus prognoza PV na planowane godziny. Limit jest trzymany ceną, nie
  zakazem — gdy zapas się kończy, odpadają najgorzej płatne sprzedaże. Atrybuty
  `pv_this_month` / `export_this_month` na `sensor.prelumo_proposed_plan`.
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

### Ciepła woda (CWU) — podbicia jako odbiornik przesuwalny

Pompa ciepła grzejąca CWU w godzinie, w której bateria sprzedaje, zabiera kWh, które poszłyby do
sieci po cenie RCE. Prelumo traktuje podbicie CWU jak ładunek, który można przesunąć w oknie:

- **Konfiguracja**: ⋮ → Zmień konfigurację → „CWU (encja climate zasobnika)” i „Temperatura zasobnika
  CWU”; w opcjach **Podbicia CWU**: `HH:00-HH:00 cel potem [peak]`, kolejne po `;`
  (domyślnie `03:00-07:00 50 40; 13:00-16:00 53 40 peak` — `peak` = tylko w dni robocze).
- **Nauka** (z historii temperatury zasobnika i mocy pompy, 10 dni, potem pamiętane 45 dni): każdy
  wzrost temperatury zasobnika to jedno grzanie; z nich kWh/°C, moc pompy przy CWU, zwykła godzina
  i temperatura startu każdego podbicia oraz profil pozostałych dogrzań. Model ogrzewania uczy się
  już bez energii CWU.
- **Plan**: dla każdej dozwolonej godziny startu optymalizator baterii liczony jest z podbiciem w tej
  godzinie; wygrywa najtańsza (przy remisie najpóźniejsza, wybór zmienia się dopiero przy zysku
  > 0,05 zł). Podbicie kończy się w oknie.
- **`sensor.prelumo_dhw_plan`** — stan: najtańszy start najbliższego podbicia; atrybut `boosts`:
  okno, energia, `best_start`, `usual_start` (kiedy podbicie zwykle startuje), `planned_start`
  (gdzie liczy je plan baterii), `saving_vs_usual`, `extra_cost_by_start`.
- **`switch.prelumo_dhw_control`** (domyślnie wł.) — Prelumo samo uruchamia podbicia: w wybranej
  godzinie ustawia na encji climate temperaturę docelową (raz — zmiana ręczna w trakcie nie jest
  nadpisywana), a gdy zasobnik ją osiągnie albo okno się skończy, ustawia temperaturę „potem”.
  Okno jest wtedy na ten dzień zrobione. Plan baterii liczy podbicie w wybranej godzinie. Bez planu
  na dane okno (np. błąd planowania) podbicie startuje w ostatniej godzinie okna; przy awarii sieci
  nie startuje. Przełącznik działa niezależnie od trybu cienia i „Sterowania automatycznego”
  (te dotyczą falownika).
- Przełącznik wyłączony = **tylko podgląd**: podbicia uruchamiają Twoje automatyzacje, plan baterii
  liczy je w zwykłej godzinie, a sensor pokazuje, ile dałoby przesunięcie.
- Własne automatyzacje ustawiające temperaturę w oknach podbić powinny mieć warunek
  `{{ not is_state('switch.prelumo_dhw_control', 'on') }}` — wtedy działają tylko, gdy Prelumo
  nie steruje.

### Odbiorniki sterowane (opcjonalne)

Ten sam mechanizm co dla CWU, dla dowolnego urządzenia, które raz na dobę ma pobrać stałą energię
w zadanym oknie. Opcja **Odbiorniki sterowane**, po jednym w wierszu:

```
nazwa HH:00-HH:00 kWh kW [encja] [peak]
zmywarka 22:00-06:00 1.2 2.0 switch.zmywarka
```

- `sensor.prelumo_flex_loads` — najbliższy start; atrybut `loads`: okno, czas pracy, `best_start`,
  `extra_cost_by_start` (o ile drożej wychodzi każda inna godzina startu), `running`.
- `switch.prelumo_load_control` (domyślnie wył.) — wyłączony: tylko podgląd, plan baterii nie liczy
  odbiornika osobno (jego zużycie jest w wyuczonym profilu domu). Włączony: plan baterii liczy
  odbiornik w wybranej godzinie, o tej godzinie encja jest włączana (`homeassistant.turn_on`)
  i wysyłane jest zdarzenie `prelumo_load_start`, po czasie kWh/kW encja jest wyłączana i idzie
  `prelumo_load_end` (dane: `name`, `entity_id`, `kwh`). Encja jest opcjonalna — urządzenie, które
  wymaga czegoś więcej niż włącz/wyłącz, obsłuż automatyzacją na tych zdarzeniach.
- Uruchomiony odbiornik nie jest już przesuwany; okno jest zrobione na dany dzień po zakończeniu.
  Historyczne zużycie urządzenia siedzi jeszcze kilka tygodni w profilu domu o dawnych godzinach.

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
| `sensor.prelumo_forecast_accuracy` | błąd godzinowy prognozy zużycia (dom + PC) z 7 dni jako % zmierzonej energii; atrybuty: `load_30_days`, `pv_7_days`, `pv_30_days` (`error_pct`, `bias_pct`: + = prognoza za wysoka), `days` |
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
