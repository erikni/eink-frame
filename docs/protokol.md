# Obrazový protokol EIF1

GET /frame.bin s Authorization: Bearer FRAME_TOKEN.
Celkem 96016 bajtů; Content-Length musí odpovídat.

| Offset | Obsah |
|---|---|
| 0–3 | ASCII EIF1 |
| 4–5 | šířka 800, uint16 big endian |
| 6–7 | výška 480, uint16 big endian |
| 8–11 | sekundy do dalšího probuzení, uint32 big endian, 60–86400 |
| 12–15 | CRC32 obou rovin, uint32 big endian |
| 16–48015 | černá bitová rovina, 48000 bajtů |
| 48016–96015 | červená bitová rovina, 48000 bajtů |

Pixely jsou zleva doprava, řádky shora dolů, MSB bit první.
Bit 0 znamená aktivní barvu, bit 1 neaktivní. Bílý pixel má v obou
rovinách 1; černý má 0 pouze v černé, červený 0 pouze v červené.
CRC odpovídá Python zlib.crc32 (IEEE polynomial 0xedb88320).
Zkontroluj celý rámec před jakoukoli změnou displeje.

GET /preview.png se stejnou autorizací vrací PNG přesně stejného rozložení.
CRC slouží ke kontrole přenosu, nikoli jako bezpečnostní podpis.
