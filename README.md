# Prelumo — one step ahead of the sun

Integracja Home Assistant planująca i sterująca harmonogramem (Time of Use) hybrydowego
falownika z magazynem energii: ładuje baterię w tanich godzinach G13, sprzedaje po RCE gdy
się opłaca, maksymalnie wykorzystuje PV i uwzględnia pompę ciepła oraz ładowanie Tesli.

Prelumo nie rozmawia z Modbusem — korzysta wyłącznie z encji i akcji HA
(domyślnie ESPHome `deye-modbus`, encje `deye_falownik_*`).

## Jak działa

```
co 30 s   odczyt harmonogramu → aktywny slot → tryb pracy (Export First tylko w slocie Sell)
co godz.  prognozy → optymalizator godzinowy (DP po SoC) → scalenie do 6 slotów
          → porównanie z falownikiem → zapis tylko przy istotnej zmianie (limit zapisów/dobę)
raz/dobę  nauka: profil zużycia, model pompy ciepła, wzorzec Tesli, korekta Solcast,
          rzeczywisty koszt wczoraj (tryb cienia)
```

| Wejście | Źródło (domyślnie) |
|---|---|
| Ceny zakupu | taryfa G13 z opcji (strefy lato/zima, weekendy i święta = pozaszczyt) |
| Ceny sprzedaży | `sensor.rce_pse_price` + `_tomorrow` (okresy 15 min → godzina) |
| PV | Solcast `detailedHourly` × współczynnik korekty (miesiąc × godzina) |
| Zużycie domu | profil dzień tygodnia × godzina z recordera (bez PC i Tesli) |
| Pompa ciepła | `sensor.sprsun_silnik_moc` — regresja stopniogodzin + profil CWU |
| Tesla | ESP32 BLE: SoC, limit, podłączenie, moc; kalendarz; ręczny cel |
| Awaria sieci | `sensor.deye_falownik_siec_napiecie_l1` < 180 V |

Priorytet zapotrzebowania Tesli: ręczny cel > kalendarz (start wydarzenia = wyjazd,
„90%” w tytule = cel) > auto podłączone (SoC → limit) > wyuczony wzorzec.
Bateria domowa nie ładuje Tesli, chyba że termin wyjazdu jest zagrożony albo jest awaria sieci.

### Limity

- **Maks. cena ładowania z sieci** — bateria ładuje się z sieci tylko, gdy cena zakupu ≤ limit
  (domyślnie 0 = najniższa cena z taryfy G13, czyli w praktyce tylko strefa pozaszczytowa).
- **Min. SoC przy sprzedaży** (domyślnie 20%) — bateria nie sprzedaje do sieci poniżej tego poziomu;
  slot Sell nigdy nie dostaje niższego progu.

### Strategie
- **Autokonsumpcja** — bateria nigdy nie oddaje do sieci; może ładować się z sieci w strefie
  pozaszczytowej, jeśli to obniża koszt.
- **Maks. handel z siecią** — bateria sprzedaje, gdy RCE to opłaca.
- **Arbitraż z siecią** (osobny przełącznik, domyślnie wył.) — czy energię pobraną z sieci wolno
  potem sprzedać. Gdy wyłączony, po ładowaniu z sieci bateria nie eksportuje, dopóki nie zejdzie
  do min SoC.

## Encje

| Encja | Opis |
|---|---|
| `sensor.prelumo_active_slot` | tryb aktywnego slotu; atrybuty: wszystkie sloty z falownika |
| `sensor.prelumo_next_change` | czas następnej zmiany slotu |
| `sensor.prelumo_proposed_plan` | plan Prelumo: sloty, plan godzinowy, koszt, decyzja zapisu |
| `sensor.prelumo_expected_savings` | oszczędność 24 h vs zwykła autokonsumpcja |
| `sensor.prelumo_shadow_savings` | suma oszczędności z trybu cienia (+ koszt rzeczywisty per dzień) |
| `sensor.prelumo_ev_energy_needed`, `_load_forecast`, `_pv_forecast_corrected` | prognozy |
| `binary_sensor.prelumo_sell_now`, `_grid_outage`, `_fallback_active` | |
| `select.prelumo_strategy` | autokonsumpcja / maks. handel |
| `switch.prelumo_auto_mode` | zgoda na sterowanie (tryb pracy i zapisy) |
| `switch.prelumo_shadow_mode` | **domyślnie włączony** — próba na sucho: liczy plan i tryb pracy, niczego nie zmienia w falowniku |
| `switch.prelumo_grid_arbitrage` | |

## Gdzie zobaczyć konfigurację i wyniki

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
