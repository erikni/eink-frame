# Měření před výběrem baterie a výrobou držáku

Hardware zde není připojený. Následující hodnoty je potřeba skutečně změřit;
rozměry z nabídky výrobce nejsou měřením vnitřního prostoru.

## Odběr celé sestavy

1. Nejprve ověř obraz a Wi-Fi při napájení USB. Potvrď označení panelu a revizi HATu.
2. Pro bateriové měření odpoj USB. Měř proud v sérii mezi chráněnou baterií
   a napájecím konektorem desky, při ověřené polaritě. Ampérmetr nikdy
   nepřipojuj přímo napříč baterií. Zapojení měň s odpojeným napájením.
3. Zaznamenej připojení Wi-Fi, stažení, obnovu a ustálený hluboký spánek
   celé sestavy. Pro malé proudy potřebuješ odpovídající rozsah nebo analyzátor
   spotřeby; běžný USB měřák klidový odběr přesně nezměří. Jeho měření na 5 V
   nelze bez přepočtu zaměnit za proud z baterie.
4. Preferuj integrovanou energii/proud za celý cyklus. Multimetr nemusí
   zachytit špičky Wi-Fi; úbytek na měřidle může vyvolat reset.
5. Teprve po ověření firmware zvaž přerušení výrobcem určeného low-power
   jumperu FireBeetle. Změř před/po; jde o fyzickou změnu desky.
6. Nabíjení měř zvlášť. Baterie nesmí být stlačená; konektor, polaritu,
   povolený nabíjecí proud a délku kabelu ověř podle skutečné baterie.

| Veličina | Naměřeno |
|---|---|
| Přesná deska, panel, HAT/revize | doplnit |
| Napětí baterie při měření | doplnit |
| Proud v deep sleep celé sestavy (mA) | doplnit |
| Průměrný proud během aktivní fáze (mA) | doplnit |
| Doba aktivní fáze Wi-Fi + obraz (s) | doplnit |
| Špičkový proud a případné resety | doplnit |
| Spotřeba jednoho cyklu (mAh) | doplnit |
| Skutečný nabíjecí proud a teplota | doplnit |

Výpočet pro N aktualizací denně:
`denní mAh = N × aktivní_mA × aktivní_s / 3600 + spánek_mA × (24 − N × aktivní_s / 3600)`.
Při aktualizaci po 30 minutách mezi 7–22 a po 2 hodinách v noci je to přibližně
35 probuzení; události přidávají další. Výdrž odhadni jako
`využitelná kapacita / denní mAh`. Pro první odhad použij 70 % jmenovité kapacity
jako konzervativní pracovní předpoklad, nikoli vlastnost baterie.

## Rámeček a držák

Rámeček rozlož a měř s paspartou i panelem v jejich zamýšlené poloze.

| Rozměr | Naměřeno (mm) |
|---|---|
| Světlá šířka × výška kapsy | doplnit |
| Hloubka od zadní plochy panelu k zavřenému zadnímu krytu | doplnit |
| Panel šířka × výška × tloušťka, místo flex kabelu | doplnit |
| Viditelná aktivní plocha / požadovaný otvor pasparty | doplnit |
| FireBeetle rozměry, montážní otvory a výška konektorů | doplnit |
| HAT rozměry, otvory a výška kabelu | doplnit |
| Baterie skutečné rozměry včetně vývodu | doplnit |
| Poloha USB-C, vypínače a vývod kabelu | doplnit |

Pracovní koncept: panel ve vlastní mělké kolébce s oporou mimo aktivní sklo,
za ním deska a HAT vedle sebe, baterie v samostatné volné kapse. Flex kabel
nesmí být přiskřípnutý ani ostře přehnutý. Kryt bude případně přesahovat
původní záda rámu. Zajisti přístup k USB-C a odpojení baterie. Prototyp nejprve
ověř z kartonu, až potom tvoř CAD a tisk. Finální STL bez těchto rozměrů
by byl odhad a mohl by poškodit panel nebo stlačovat baterii.

## Rozhodnutí po měření

Chráněná LiPo 2000 mAh je kandidát, ne potvrzená finální velikost.
Pokud spotřeba nestačí pro požadovanou výdrž, nejprve prodluž intervaly,
prověř klidový odběr HATu a případné odpojení jeho napájení vhodným obvodem
(zamezit napájení přes GPIO). Větší baterii vyber až s vypočtenou spotřebou
 a skutečným volným prostorem. Nepřidávej náhodný boost nebo druhou nabíječku
k desce s vlastní nabíjecí částí. Pro finální volbu doplň i požadovanou výdrž
mezi nabíjeními.
