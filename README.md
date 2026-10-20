# WLAN Pi Remote

Lokale Windows-GUI die het front panel van de WLAN Pi nabootst: je ziet live
exact hetzelfde beeld als op het schermpje en je bedient de knoppen (D-pad +
KEY1/2/3) met de muis of het toetsenbord.

## Starten

Dubbelklik `start.bat`. De eerste keer wordt automatisch een `.venv`
aangemaakt met `paramiko` en `Pillow`. Of handmatig:

```
pip install -r requirements.txt
python app.py
```

Vul host (bijv. `wlanpi.local`, het IP-adres, of `ip:poort`), gebruiker
(`wlanpi`) en wachtwoord in en klik **Verbinden**. SSH-keys werken ook; het
wachtwoord is dan alleen nodig als `sudo` erom vraagt.

Bediening: pijltjestoetsen, Enter/spatie = midden, 1/2/3 = KEY1-3.
**Screenshot** slaat het huidige scherm op als PNG (4x vergroot).

## Hoe het werkt

1. `app.py` maakt via SSH verbinding en zet `bridge.py` in `/tmp` op de Pi.
2. `bridge.py` draait met `sudo`, stopt de service `wlanpi-fpms` en start FPMS
   zelf in de ingebouwde knop-emulatiemodus (`fpms -e`).
3. Elke keer dat FPMS het scherm tekent (`oled.drawImage`) stuurt de bridge
   het beeld als PNG naar de GUI. Knoppen uit de GUI gaan als toetsaanslagen
   terug naar de emulator van FPMS.
4. Het fysieke scherm en de fysieke knoppen blijven gewoon werken.
5. Bij afsluiten, bij een weggevallen verbinding of na 15 s zonder heartbeat
   stopt de bridge en start `wlanpi-fpms` vanzelf weer op.

Er wordt niets permanent op de Pi geïnstalleerd of aangepast.
