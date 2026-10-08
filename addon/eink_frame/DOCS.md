# Instalace na Home Assistant OS

1. Na počítači s tímto projektem spusť `python3 tools/package_addon.py`.
2. Složku `output/local-addon/eink_frame` zkopíruj do sdílené složky
   `/addons/eink_frame` zařízení s HA OS, například přes Samba share nebo SSH
   doplněk se zpřístupněnou složkou addons. Nekopíruj ji do `/config`.
3. V Nastavení → Aplikace (v některých verzích Doplňky) → Obchod aktualizuj
   seznam. V části místních aplikací vyber E-ink Frame a nainstaluj.
4. V konfiguraci nastav `frame_token` na náhodný řetězec alespoň 24 znaků.
   Ostatní entity jsou již přednastavené. `demo: true` nejdřív slouží k ověření
   displeje. Pro skutečnou agendu a stavy nastav `demo: false`.
5. Spusť aplikaci a zkontroluj log. Interní Home Assistant API autorizuje
   Supervisor; dlouhodobý HA token není potřeba ručně vytvářet.
6. Firmware musí mít stejný FRAME_TOKEN a adresu
   `http://IP_ADRESA_HA:8080/frame.bin`. ESP32 musí dosáhnout na tuto adresu
   přes domácí Wi-Fi. Port lze v síťovém nastavení aplikace změnit; změň pak URL.
7. Zapni start při spuštění systému. Při úpravě zdrojů opakuj zabalení,
   překopírování a přestavění místní aplikace; samotný restart zdroje nepřestaví.

Aplikace zatím podporuje amd64 a aarch64. Živá instalace v HA OS zde nebyla
provedena. Port je určen pro domácí LAN; používá HTTP a Bearer token, bez TLS.
Nevystavuj jej do internetu. Po změně konfigurace restartuj aplikaci.

Kalendář čte seznam událostí přes REST, nikoli jen atribut příští události.
Google kalendář přidej standardní integrací v Home Assistantu a jeho entitu
přidej do `calendar_entities`. Denní maximum zobrazuje z input_number.outdoor_effective_temperature;
výpočet nebo načítání předpovědi musí zajistit automatizace v HA.

## Vzhled a prázdný kalendář

Písmo Lato je součástí obrazu aplikace. Hlavní událost je tučná, číslo dne
bez tečky. Bez událostí se zobrazuje citát a autor, které lze změnit v
`empty_calendar_quote` a `empty_calendar_author`. Pokud načtení selže,
zobrazí se chyba. Při `demo: true` přepínač `demo_empty_calendar: true`
ukáže náhled bez událostí; v živém režimu o stavu rozhodují skutečná data.

Ukončené události se nezobrazují. Probíhající události mají červený podklad
a bílý text. Černý pruh s datem a teplotami je vždy přes celou výšku.
