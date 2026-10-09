# uma8-callmic

Macht aus dem miniDSP UMA-8 (Raw-Firmware) ein gutes Telefonie-Mikrofon unter Linux:
Echounterdrückung, Beamforming über 7 Mikrofone, optionale Hallunterdrückung,
DeepFilterNet-Rauschunterdrückung, volle Bandbreite. Bedienung über ein KDE-Tray-Icon.

## Voraussetzungen

- Linux mit PipeWire (ab 1.2) und WirePlumber (getestet: Fedora 44 KDE; Arch-Pakete im Container gebaut und geprüft)
- UMA-8 mit **Raw-Firmware** (`micArray_vf_raw_v1.3_up.bin`, USB-ID `2752:001d`). Firmware nur mit dem
  offiziellen miniDSP-Tool wechseln (Windows-VM: siehe `firmware/docker-compose.usb.yml`).

## Installation (Fedora, RPM)

Die RPMs entstehen in einem Podman-Container aus Fedora 44 – auf dem Host braucht es nur `podman`, `git` und
`python3`, gebaut wird nichts ins System. Vorher `./uninstall.sh`, falls `install.sh` benutzt wurde (Einstellungen
behalten): Plugin und Dienst der Entwickler-Installation gingen sonst denen des Pakets vor.

```sh
./packaging/build-rpms.sh                      # beide Pakete; nur eins: ./packaging/build-rpms.sh uma8-callmic
sudo dnf install packaging/out/*.x86_64.rpm
```

Das ergibt zwei Pakete:

- `uma8-callmic`: Tray-Programm (`/usr/bin/uma8-callmic`), Plugin `libuma8_beam.so` in `/usr/lib64/ladspa`,
  Benutzerdienst `uma8-callmic-chain.service`, Startmenü-Eintrag
- `deepfilternet-ladspa`: DeepFilterNet 0.5.6 als LADSPA-Plugin, mit behobenem Thread-Leck und Latenzabbau
  (siehe [Latenz](#latenz))

Danach „UMA-8 Call Mic“ aus dem Startmenü starten. Beim ersten Start aktiviert das Tray den Dienst und den
Autostart für den eigenen Benutzer, nur dieses eine Mal: Ein später per `systemctl --user disable` abgeschalteter
Dienst oder ein gelöschter Autostart-Eintrag bleibt so (Autostart: Optionen → „Beim Login starten“). Das Mikrofon
„UMA-8 Call Mic“ einmal in den Audio-Einstellungen als Standard wählen (oder `pactl set-default-source uma8_callmic`).

Der Bau lädt den DeepFilterNet-Quelltext (Prüfsumme in `packaging/deepfilternet-ladspa.sources`) und die
Rust-Crates herunter, `rpmbuild` selbst läuft danach offline. Ergebnisse, Logs und rpmlint-Ausgabe:
`packaging/out/`.

## Installation (Arch Linux, Paket)

Zwei Pakete, gebaut aus dem Checkout: `deepfilternet-ladspa` (DeepFilterNet 0.5.6 mit den Patches gegen Thread-Leck und wachsende Latenz) und `uma8-callmic`.
Vorher `./uninstall.sh`, falls `install.sh` benutzt wurde (Einstellungen behalten): Plugin und Dienst der
Entwickler-Installation gingen sonst denen des Pakets vor.

    ./packaging/build-arch.sh            # baut beide in einem Podman-Container (archlinux:latest)
    sudo pacman -U packaging/out/arch/*.pkg.tar.zst

Direkt auf dem Arch-Rechner geht es auch, im jeweiligen Verzeichnis (zuerst das Plugin):

    cd packaging/arch/deepfilternet-ladspa && makepkg -si
    cd ../uma8-callmic && makepkg -si

`deepfilternet-ladspa` ersetzt das AUR-Paket `deepfilternet-plugin-pipewire-bin` (provides/conflicts/replaces): pacman tauscht es beim Installieren aus. Das AUR-Paket liefert zwar `libdeep_filter_ladspa` und erfüllt damit die Abhängigkeit von `uma8-callmic`, behält aber Thread-Leck und wachsende Latenz. Abhängigkeiten kommen aus den offiziellen Repos: `pipewire`, `pipewire-audio` (pw-record, WebRTC-AEC), `libpipewire` (echo-cancel), `libpulse` (pactl), `pyside6`, `python-numpy`.

Erster Start: Das Paket aktiviert nichts von selbst. Beim ersten Start des Tray-Programms (`uma8-callmic` oder Startmenü-Eintrag) richtet es Benutzerdienst und Autostart ein, nur dieses eine Mal (wie oben bei Fedora). Der Dienst `uma8-callmic-chain.service` ist ein systemd-Benutzerdienst (`systemctl --user status uma8-callmic-chain`); er schreibt vor dem Start die PipeWire-Konfiguration neu (`uma8-callmic --write-config`).

## Entwickler-Installation (ohne Paket)

Läuft direkt aus dem Repo, nur für den eigenen Benutzer, und verweigert sich, solange das Paket `uma8-callmic`
(RPM oder Arch) installiert ist. Braucht Rust, PySide6, numpy und ein DeepFilterNet-Plugin:

- Fedora: `sudo dnf install cargo python3-pyside6 python3-numpy pipewire-utils pulseaudio-utils`, dazu
  `./packaging/build-rpms.sh deepfilternet-ladspa` und `sudo dnf install packaging/out/deepfilternet-ladspa-*.x86_64.rpm`
- Arch: `sudo pacman -S rust pyside6 python-numpy`, dazu das Paket `deepfilternet-ladspa` von oben
  (`./packaging/build-arch.sh deepfilternet-ladspa` und `sudo pacman -U packaging/out/arch/deepfilternet-ladspa-*.pkg.tar.zst`).
  Alternative: `yay -S deepfilternet-plugin-pipewire-bin`, behält aber Thread-Leck und wachsende Latenz.

```sh
./install.sh
uma8-callmic &
```

`install.sh` beendet ein laufendes Tray, bevor es die Kette neu startet, und startet es danach wieder (in einer
grafischen Sitzung).

Die Plugins werden in dieser Reihenfolge gesucht: `~/.local/lib/ladspa`, `/usr/lib64/ladspa` (entfällt, wenn es nur
ein anderer Name für `/usr/lib/ladspa` ist, wie auf Arch), `/usr/lib/ladspa`; ist keins installiert, gilt der
Systemort der Distribution. `UMA8_BEAM_PLUGIN` bzw. `UMA8_DFN_PLUGIN` erzwingen einen bestimmten Pfad. Ist das
gefundene `libuma8_beam.so` älter als das Programm (es fehlen Controls), wird das Icon rot und der Tooltip nennt
den Pfad.

Beim ersten Start prüft das Programm die Kanalzuordnung (10 s still sein) und bittet danach um eine
Kalibrierung (Rechtsklick → Kalibrieren…).

## Aktualisieren von einer älteren Version

- Entwickler-Installation: neuen Stand holen und `./install.sh` erneut ausführen. Es beendet vorher ein laufendes
  Tray – ein altes Tray setzte sonst seine alten Werte in die neue Kette (z. B. die volle Verstärkung zusätzlich zu
  den 18 dB vor der Echounterdrückung) – startet die Kette neu und das Tray wieder. Läuft das Tray nicht, danach
  `uma8-callmic &` oder Startmenü.
- Wechsel zum Paket (RPM oder Arch): erst `./uninstall.sh` (Einstellungen behalten), dann das Paket installieren und
  „UMA-8 Call Mic“ aus dem Startmenü starten; es richtet Dienst und Autostart neu ein. Liegen noch Reste von
  `install.sh` herum (`~/.local/lib/ladspa/libuma8_beam.so`, `~/.config/systemd/user/uma8-callmic-chain.service`,
  `~/.local/bin/uma8-callmic`), wird das Icon rot und nennt sie.
- Einmalige Umstellung der Einstellungen: Die Hallunterdrückung ist jetzt standardmäßig an und ihre Stärke auf den
  Standard zurückgesetzt (die Stärke wirkt jetzt anders). Das Tray speichert das beim ersten Start und zeigt einen
  Hinweis; abschalten unter Optionen.
- Neu und standardmäßig an: die Echounterdrückung (unten).

## Bedienung

- Linksklick aufs Icon: aktiv ↔ deaktiviert (Rohsignal)
- Rechtsklick: Aktiv, Kalibrieren…, Arbeitsplatz einmessen…, Platzierung…, Optionen…, Beenden
- Icon grün = aktiv, grau = deaktiviert, rot = Problem (Tooltip zeigt die Ursache)
- „Automatisch nachführen“ (Optionen → Richtung) hört nur zu, solange ein Programm „UMA-8 Call Mic“ aufnimmt.
  Ohne Anruf bleibt der Strahl, wo er zuletzt war, und die Kette schläft (Tooltip: „ruht ohne Aufnahme“).

### Arbeitsplatz einmessen (optional)

Für einen festen Schreibtisch mit Lautsprechern. Ohne Profil verhält sich alles wie bisher.

1. Rechtsklick → **Arbeitsplatz einmessen…**
2. **Lautsprecher:** Lautstärke wie in einem Anruf einstellen (nicht lauter), still sein, „Lautsprecher messen“.
   Jeder Lautsprecher spielt 2,5 s Rauschen über die Standardausgabe, erst links, dann rechts; das UMA-8 wird
   dabei direkt aufgenommen. Kommt nichts an (Lautstärke 0, Kopfhörer, HDMI als Standardausgabe), sagt der
   Assistent das. Mit Kopfhörern: überspringen.
3. **Sprechrichtung:** die gewohnte Kalibrierung (oder die bisherige behalten).
4. **Tastatur** (optional): 5 s tippen. Nur zur Anzeige.
5. **Ergebnis:** Richtungen im Polardiagramm, „Bewegungsbereich“ (wie weit du dich beim Sprechen bewegst) und
   „Lautsprecher ausblenden (Nullstellen)“, dann Speichern.

Wirkung: Die automatische Nachführung folgt nur innerhalb des Bewegungsbereichs um die kalibrierte Richtung und
nie in Richtung eines Lautsprechers (±20°). Die Nullstellen sind aus, solange du sie nicht einschaltest: Sie
dämpfen den Direktschall der Lautsprecher deutlich, das Echo insgesamt aber nur wenig (Reflexionen überwiegen).
Am besten im Anruf vergleichen; der Schalter steht auch unter Optionen. „Profil löschen“ auf der ersten Seite
des Assistenten setzt alles zurück.

**Platzierung…** zeigt live, woher gerade Schall kommt, den Strahl bei 1 und 3 kHz, die Marker und die Pegel
(deine Stimme, Grundrauschen, Lautsprecher) mit Hinweisen wie „Mikrofon näher zu dir“. „Lautsprecher neu
messen…“ wiederholt nur die Lautsprechermessung, um Aufstellungen zu vergleichen; „Messung übernehmen“ speichert
sie. Die Aufnahme dafür läuft nur, solange das Fenster offen ist.

## Echounterdrückung

Entfernt aus allen 7 Mikrofonen, was die Lautsprecher abspielen, bevor Beamforming und Rauschunterdrückung
laufen. Das Gegenüber hört sich nicht mehr selbst, und sein Anruf-Programm schaltet bei Gegensprechen nicht
mehr das eigene Mikrofon stumm. Als Referenz dient die jeweilige Standardausgabe (PipeWire-Modul `echo-cancel`,
WebRTC AEC3); ein Wechsel der Standardausgabe wird übernommen, auch mitten im Anruf.

- Die Echounterdrückung der Anruf-Programme kann an bleiben.
- Nur Ton auf der Standardausgabe wird entfernt. Gibt das Anruf-Programm auf einem anderen Gerät aus, bleibt
  dessen Echo.
- Die Referenz ist stereo: Von einer Mehrkanal-Standardausgabe (z. B. 5.1) zählen nur vorne links und rechts;
  Ton über Mitte, Subwoofer oder hinten (auch hochgemischter Stereo-Ton) bleibt als Echo.
- Spricht man gleichzeitig mit dem Gegenüber, wird die eigene Stimme leiser, umso mehr, je lauter die
  Lautsprecher am Mikrofon ankommen. Lautsprecher leiser oder weiter weg hilft.
- Die Referenz ist nur verbunden, solange ein Programm von „UMA-8 Call Mic“ aufnimmt: Musik und Videos ohne
  Anruf wecken die Kette nicht. Das erledigt ein kleiner Helfer (`uma8-callmic --ref-linker`), der mit dem
  Dienst startet und endet; er verbindet beim Start einer Aufnahme nach rund 50 ms und trennt 2 s nach ihrem
  Ende. Läuft er nicht, funktioniert das Mikrofon weiter, nur ohne Echounterdrückung.
- „Verstärkung“ bleibt die Gesamtverstärkung; 18 dB davon liegen vor der Echounterdrückung.
- Ausschalten: Optionen → „Echounterdrückung (Lautsprecher)“. Das startet die Filterkette neu (kurze
  Tonpause). Ohne Tray: `echo_cancel = false` in `~/.config/uma8-callmic/config.toml`, dann
  `systemctl --user restart uma8-callmic-chain`.

## Aufbau

```
UMA-8 (7 Mikros) ─► +18 dB ─► Echounterdrückung (Referenz: Standardausgabe, nur während einer Aufnahme)
   ─► uma8_beam (Beamforming, Hall) ─► DeepFilterNet ─┐
      └─ Roh-Weg (Mittel-Mikrofon, latenzangeglichen) ─┴► Umschalter ─► Begrenzer ─► „UMA-8 Call Mic“
```

Ohne Echounterdrückung liest `uma8_beam` das UMA-8 direkt und verstärkt allein. Die Zwischenstufen
(`uma8_callmic_pre`, `uma8_callmic_aec`) sind interne Quellen und tauchen in keiner Geräteliste auf.

Die Tonverarbeitung läuft als PipeWire-Filterkette im Dienst `uma8-callmic-chain` (Rust-LADSPA-Plugin in
`plugin/`). Vor jedem Start schreibt der Dienst die Kettenkonfiguration aus den Einstellungen
(`uma8-callmic --write-config`). Das Tray-Programm (`uma8_callmic/`) steuert nur und ist nie im Tonweg.

## Latenz

Soll: Echounterdrückung (nur wenn an) + Beamforming 21,3 ms + DeepFilterNet 30 ms + Begrenzer 5 ms
(`uma8_callmic/constants.py`), mit Echounterdrückung insgesamt etwa 86 ms. DeepFilterNet hat 20 ms mit einem Frame
Ausgabepuffer; die Kette setzt „Min Processing Buffer (frames)“ auf 1, also 10 ms mehr Puffer. Der Worker-Thread
des Plugins liefert auf einem ausgelasteten Desktop manchmal mehr als 10 ms zu spät, mit nur einem Frame pendelte
die Latenz dann alle 10 s zwischen 10 und 20 ms Puffer, mit einem Aussetzer bei jedem Rückbau. Über den
Mindestpuffer hinaus puffert DeepFilterNet nach jedem Aussetzer (Underrun, etwa unter CPU-Last) 10 ms mehr und
baut das nach 10 s ohne Aussetzer wieder ab, danach 10 ms je Sekunde; höchstens 1 s. Upstream 0.5.6 baute nie
wirklich ab, die Latenz wuchs über Tage auf Sekunden (behoben ab `deepfilternet-ladspa` 0.5.6-2), und der
Mindestpuffer galt erst nach dem ersten Underrun (ab 0.5.6-4 von Anfang an; beides
`packaging/deepfilternet-0.5.6-ladspa-latency.patch`). Jede Änderung steht im Journal:

```sh
journalctl --user -u uma8-callmic-chain | grep -i latency
```

Messen gegen das Roh-Array, gesamt und je Stufe (Vorverstärkung, Echounterdrückung, Ausgang):

```sh
python3 tools/latency_probe.py                    # 60 s, alle 10 s ein Wert je Stufe; dabei sprechen
python3 tools/latency_probe.py --max-excess-ms 10 # Rückschrittprüfung: Exit 1, wenn der Ausgang >10 ms über dem Soll liegt
python3 tools/latency_probe.py --expect-max-ms 90 # absolut: Exit 1, wenn der Median darüber liegt
```

Ein pw-record-Stream nimmt alle Stufen im selben Graphzyklus auf, der Versatz zwischen den Spalten ist also die
echte Latenz. Er fordert das Quantum eines Anrufs an (mit Echounterdrückung 480 Samples, sonst `clock.quantum`); PipeWire nimmt
aber das kleinste Quantum aller aktiven Knoten. Deshalb liest er das tatsächliche Quantum nach dem Verbinden und
am Ende aus `pw-top` und gibt es aus („unbekannt“, wenn es sich nicht ermitteln lässt). Den Ausgang misst das Werkzeug über Pegelwechsel: währenddessen sprechen, Fenster mit Stille bleiben
leer. Der Ton bleibt im Speicher, ausgegeben werden nur Zahlen. Die Zusammenfassung zeigt die Abweichung vom Soll;
positiv heißt zusätzlich gepuffert.

Die absolute Gesamtlatenz hängt vom Aufbau ab. Die Echounterdrückung misst hier etwa 30 ms (Quantum 256, kabelgebundener
Ausgang, Fedora mit webrtc-audio-processing 2.1; insgesamt etwa 86 ms), auf einem zweiten Aufbau (Arch mit
webrtc-audio-processing 1.3) etwa 41 ms, bei gleichem Quantum und auch mit kabelgebundenem Ausgang; vermutlich liegt es
an der WebRTC-Version (nicht gegengeprüft). PipeWire
rundet das angeforderte 480 standardmäßig auf eine Zweierpotenz ab (256, `clock.power-of-two-quantum`). Als Rückschrittprüfung (etwa DeepFilterNet-Pufferwachstum)
taugt darum `--max-excess-ms 10`: Es vergleicht den Ausgang mit dem Soll aus der gemessenen Echounterdrückung und ist
vom Aufbau unabhängig. `--expect-max-ms` ist ein fester Wert für einen bekannten Aufbau. Exit: 0 in Ordnung,
1 Schwelle überschritten oder nicht prüfbar, 2 Knoten fehlt oder Aufnahme scheitert.

## Tests

```sh
python -m pytest                                  # Unit-Tests
cargo test --manifest-path plugin/Cargo.toml      # Rust-Tests
python -m pytest -m integration                   # nach der Installation, braucht PipeWire
python3 tools/check_output.py                     # mit angeschlossenem UMA-8
python3 tools/latency_probe.py --max-excess-ms 10 # mit laufender Kette, dabei sprechen (siehe Latenz)
```

Die RPM- und Arch-Bauten führen die Unit- und Rust-Tests beider Pakete ebenfalls aus (`%check`, `check()`), ohne
die Rechenzeit-Tests (`-m timing`), weil Bauhosts beliebig langsam sein können.

## Deinstallation

```sh
./uninstall.sh                                    # Einrichtung des eigenen Benutzers (Dienst, Autostart, …)
sudo dnf remove uma8-callmic deepfilternet-ladspa # bei RPM-Installation zusätzlich
sudo pacman -R uma8-callmic deepfilternet-ladspa  # bzw. bei den Arch-Paketen
```
