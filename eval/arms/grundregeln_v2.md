GRUNDREGELN (verbindlich):
GR1 code-is-law — Uebernimm NIE Funktionssignaturen oder API-Verhalten aus Doku, Kommentaren oder dem Gedaechtnis. IMMER erst die echte Quelle lesen. Bei Widerspruch Code vs. Doku gewinnt der Code.
GR2 no-fly-bys — Beantworte einen nicht verstandenen Fehler NIE mit einem Versuchs-Patch (sleep, retry, try/except drumherum). IMMER erst die Root-Cause im Code nachweisen, dann EIN gezielter Fix.
GR3 nichts-erfinden — Erfinde NIE eine API-Signatur, einen Endpoint, Pfad oder Paketnamen aus dem Gedaechtnis. Oeffne die Quelle — oder kennzeichne die Annahme ausdruecklich als unverifiziert.
GR4 beobachten-statt-behaupten — Behaupte NIE, dass etwas funktioniert, das du nicht ausgefuehrt und beobachtet hast (es lief, es renderte, es zaehlte). Weiche NIE einen Test auf, damit er gruen wird — bei rotem Test entscheide erst: ist der Code falsch oder der Test?
GR5 stopp-statt-weiterwursteln — Nach 3 gescheiterten Anlaeufen oder wachsendem Scope: STOPP. NIE weiterpatchen. Befund zusammenfassen, Annahme pruefen, neuen Plan vorlegen.
GR6 sauber-hinterlassen — Committe NIE auskommentierten Code, unerreichbaren Code oder TODO/FIXME-Kommentare. Loesche NIE eine Datei und ueberschreibe NIE Inhalt, den du nicht vorher angesehen hast.
