/*
 * MuseScore Studio 4.7.x batch helper.
 *
 * This extension intentionally uses API v1 compatibility because MuseScore
 * Studio 4.7 still ships it and it exposes the low-level score/element API
 * needed for selective per-staff editing. Python refuses unverified major/minor
 * versions by default.
 */

var Log = require("MuseApi.Log");

var REPORT_TAG = "ensembleTransposerReport";
var TRACKS_PER_STAFF = 4;

function asString(v) {
    if (v === undefined || v === null) {
        return "";
    }
    return String(v);
}

function listLength(list) {
    if (!list || list.length === undefined || list.length === null) {
        return 0;
    }
    return Number(list.length);
}

function pad3(n) {
    var s = String(n);
    while (s.length < 3) {
        s = "0" + s;
    }
    return s;
}

function pushUniqueNumber(arr, value) {
    for (var i = 0; i < arr.length; ++i) {
        if (arr[i] === value) {
            return;
        }
    }
    arr.push(value);
}

function partDisplayName(part) {
    if (!part) {
        return "";
    }
    var candidates = [part.partName, part.longName, part.shortName, part.instrumentId];
    for (var i = 0; i < candidates.length; ++i) {
        var s = asString(candidates[i]).trim();
        if (s.length > 0) {
            return s;
        }
    }
    return "";
}

function instrumentDisplayName(part) {
    if (!part) {
        return "";
    }
    var candidates = [part.longName, part.partName, part.instrumentId];
    for (var i = 0; i < candidates.length; ++i) {
        var s = asString(candidates[i]).trim();
        if (s.length > 0) {
            return s;
        }
    }
    return "";
}

function masterStaffIndexForExcerptStaff(masterScore, excerptStaff) {
    try {
        if (!excerptStaff || !excerptStaff.part) {
            return -1;
        }
        var excerptPart = excerptStaff.part;
        var masterPart = excerptPart.masterPart ? excerptPart.masterPart : excerptPart;
        for (var i = 0; i < masterScore.nstaves; ++i) {
            var ms = masterScore.staves[i];
            if (!ms || !ms.part) {
                continue;
            }
            if (typeof ms.part.is === "function" && ms.part.is(masterPart)) {
                return i;
            }
            if (typeof masterPart.is === "function" && masterPart.is(ms.part)) {
                return i;
            }
            if (asString(ms.part.instrumentId) === asString(masterPart.instrumentId)
                    && asString(ms.part.partName) === asString(masterPart.partName)) {
                return i;
            }
        }
    } catch (e) {
        return -1;
    }
    return -1;
}

function collectExcerptInfo(score) {
    var result = [];
    var excerpts = score.excerpts;
    for (var i = 0; i < listLength(excerpts); ++i) {
        var ex = excerpts[i];
        var item = {
            index: i + 1,
            title: asString(ex.title),
            staffs: []
        };
        try {
            var ps = ex.partScore;
            if (ps) {
                for (var j = 0; j < ps.nstaves; ++j) {
                    var st = ps.staves[j];
                    var voices = [];
                    if (st && typeof st.isVoiceVisible === "function") {
                        for (var v = 0; v < 4; ++v) {
                            if (st.isVoiceVisible(v)) {
                                voices.push(v + 1);
                            }
                        }
                    }
                    item.staffs.push({
                        excerptStaffIndex: j + 1,
                        masterStaffIndex: masterStaffIndexForExcerptStaff(score, st) + 1,
                        name: st && st.part ? partDisplayName(st.part) : "",
                        instrument: st && st.part ? instrumentDisplayName(st.part) : "",
                        visibleVoices: voices
                    });
                }
            }
        } catch (e) {
            item.mappingWarning = "Staff-Zuordnung konnte nicht vollständig gelesen werden: " + asString(e);
        }
        result.push(item);
    }
    return result;
}

function collectVoiceUsage(score, staffIndex) {
    var used = [false, false, false, false];
    var seg = score.firstSegment();
    while (seg) {
        for (var v = 0; v < 4; ++v) {
            var el = seg.elementAt(staffIndex * TRACKS_PER_STAFF + v);
            if (el && (el.type === Element.CHORD || el.type === Element.REST)) {
                used[v] = true;
            }
        }
        seg = seg.next;
    }
    var voices = [];
    for (var i = 0; i < 4; ++i) {
        if (used[i]) {
            voices.push(i + 1);
        }
    }
    return voices;
}

function clefLabel(value) {
    if (value === ClefType.G) {
        return "Violinschlüssel";
    }
    if (value === ClefType.F) {
        return "Bassschlüssel";
    }
    return "anderer Schlüssel (" + value + ")";
}

function targetPreset(target) {
    if (target === "Bb") {
        return { target: "Bb", semitones: 2, diatonicSteps: 1, keyDelta: 2 };
    }
    if (target === "Eb") {
        return { target: "Eb", semitones: 9, diatonicSteps: 5, keyDelta: 3 };
    }
    return { target: "C", semitones: 0, diatonicSteps: 0, keyDelta: 0 };
}

function targetForClef(clefValue, trebleTarget, bassTarget) {
    if (clefValue === ClefType.G) return trebleTarget;
    if (clefValue === ClefType.F) return bassTarget;
    return "C";
}

function analyzeStaff(score, staffIndex, trebleTarget, bassTarget, warnings) {
    var staff = score.staves[staffIndex];
    var zero = fraction(0, 1);
    var clefValues = [];
    var firstClef = staff.clefType(zero);
    pushUniqueNumber(clefValues, firstClef);

    var seg = score.firstSegment();
    while (seg) {
        var c = staff.clefType(seg.fraction);
        pushUniqueNumber(clefValues, c);
        seg = seg.next;
    }

    var name = staff.part ? partDisplayName(staff.part) : "";
    var instrument = staff.part ? instrumentDisplayName(staff.part) : "";
    var staffTarget = targetForClef(firstClef, trebleTarget, bassTarget);
    var item = {
        index: staffIndex + 1,
        name: name,
        instrument: instrument,
        clef: clefLabel(firstClef),
        clefValue: firstClef,
        clefValues: clefValues,
        voicesUsed: collectVoiceUsage(score, staffIndex),
        target: staffTarget,
        decision: "unverändert"
    };

    if (clefValues.length !== 1) {
        item.decision = "NICHT TRANSPONIEREN";
        item.reason = "Schlüsselwechsel erkannt";
        warnings.push("Staff " + (staffIndex + 1) + " besitzt einen Schlüsselwechsel und wird nicht automatisch transponiert.");
        return item;
    }

    if (firstClef === ClefType.G || firstClef === ClefType.F) {
        item.decision = staffTarget === "C" ? "unverändert" : "TRANSPONIEREN → " + staffTarget;
        return item;
    }

    item.decision = "NICHT TRANSPONIEREN";
    item.reason = "ungewöhnlicher oder unbekannter Schlüssel";
    warnings.push("Staff " + (staffIndex + 1) + " verwendet keinen eindeutig unterstützten G- oder F-Schlüssel und wird nicht automatisch transponiert.");
    return item;
}

function buildBaseReport(score, trebleTarget, bassTarget) {
    var warnings = [];
    var report = {
        schema: 2,
        targets: { treble: trebleTarget, bass: bassTarget },
        target: trebleTarget === bassTarget ? trebleTarget : ("G=" + trebleTarget + ",F=" + bassTarget),
        scoreName: asString(score.scoreName),
        scoreTitle: asString(score.title),
        museScoreSavedVersion: asString(score.mscoreVersion),
        nstaves: score.nstaves,
        staves: [],
        excerpts: [],
        warnings: warnings,
        status: "OK"
    };

    for (var i = 0; i < score.nstaves; ++i) {
        report.staves.push(analyzeStaff(score, i, trebleTarget, bassTarget, warnings));
    }
    report.excerpts = collectExcerptInfo(score);
    if (report.excerpts.length === 0) {
        warnings.push("Keine vorhandenen Parts/Auszüge gefunden.");
    }
    if (warnings.length > 0) report.status = "WARNUNG";
    return report;
}

function writeReport(score, report) {
    score.setMetaTag(REPORT_TAG, JSON.stringify(report));
    Log.info("ensemble-transposer: " + report.status + " target=" + report.target + " score=" + report.scoreName);
}

function notePlanFor(note, semitones, diatonicSteps) {
    var pitch = Number(note.pitch);
    var tpc1 = Number(note.tpc1);
    var tpc2 = Number(note.tpc2);
    if (!isFinite(pitch) || !isFinite(tpc1) || !isFinite(tpc2)) {
        throw new Error("Note besitzt ungültige Pitch/TPC-Daten.");
    }
    if (tpc1 !== tpc2) {
        throw new Error("tpc1 und tpc2 unterscheiden sich; Staff ist vermutlich bereits transponierend notiert.");
    }
    var newPitch = pitch + semitones;
    if (newPitch < 0 || newPitch > 127) {
        throw new Error("Transposition würde den MIDI-Pitch-Bereich verlassen.");
    }
    var newTpc = transposeTpc(tpc1, pitch, semitones, diatonicSteps);
    return { note: note, pitch: newPitch, tpc1: newTpc, tpc2: newTpc };
}

function transposeTpc(tpc, pitch, semitones, diatonicSteps) {
    var naturalTpc = [14, 16, 18, 13, 15, 17, 19]; // C D E F G A B
    var naturalPc = [0, 2, 4, 5, 7, 9, 11];
    var mod = ((tpc % 7) + 7) % 7;
    var modToLetter = {0: 0, 2: 1, 4: 2, 6: 3, 1: 4, 3: 5, 5: 6};
    var oldLetter = modToLetter[mod];
    if (oldLetter === undefined) throw new Error("TPC kann keinem diatonischen Notennamen zugeordnet werden: " + tpc);
    var oldAlter = (tpc - naturalTpc[oldLetter]) / 7;
    if (Math.floor(oldAlter) !== oldAlter || oldAlter < -2 || oldAlter > 2) {
        throw new Error("Nicht unterstützte TPC-Schreibweise: " + tpc);
    }

    var newLetter = (oldLetter + diatonicSteps) % 7;
    var newPc = ((pitch + semitones) % 12 + 12) % 12;
    var diff = newPc - naturalPc[newLetter];
    while (diff > 6) diff -= 12;
    while (diff < -6) diff += 12;
    if (diff < -2 || diff > 2) throw new Error("Zielschreibweise würde mehr als Doppelvorzeichen benötigen.");
    return naturalTpc[newLetter] + 7 * diff;
}

function collectChordNotePlans(chord, semitones, diatonicSteps, notePlans) {
    if (!chord) return;
    var notes = chord.notes;
    for (var n = 0; n < listLength(notes); ++n) notePlans.push(notePlanFor(notes[n], semitones, diatonicSteps));
    var graces = chord.graceNotes;
    for (var g = 0; g < listLength(graces); ++g) collectChordNotePlans(graces[g], semitones, diatonicSteps, notePlans);
}

function collectStaffPlan(score, staffIndex, preset) {
    var staff = score.staves[staffIndex];
    var notePlans = [];
    var keyPlans = [];
    var seg = score.firstSegment();
    var initialKeySigFound = false;

    while (seg) {
        for (var v = 0; v < 4; ++v) {
            var el = seg.elementAt(staffIndex * TRACKS_PER_STAFF + v);
            if (el && el.type === Element.CHORD) collectChordNotePlans(el, preset.semitones, preset.diatonicSteps, notePlans);
        }

        var ks = seg.elementAt(staffIndex * TRACKS_PER_STAFF);
        if (ks && ks.type === Element.KEYSIG) {
            if (Number(seg.tick) === 0) initialKeySigFound = true;
            var oldActual = Number(ks.actualKey);
            var oldConcert = Number(ks.concertKey);
            if (!isFinite(oldActual)) oldActual = Number(staff.key(seg.fraction));
            if (!isFinite(oldConcert)) oldConcert = oldActual;
            var newActual = oldActual + preset.keyDelta;
            var newConcert = oldConcert + preset.keyDelta;
            if (newActual < -7 || newActual > 7 || newConcert < -7 || newConcert > 7) {
                throw new Error("Zieltonart läge außerhalb des unterstützten Bereichs von -7 bis +7 Vorzeichen.");
            }
            keyPlans.push({ element: ks, actualKey: newActual, concertKey: newConcert });
        }
        seg = seg.next;
    }

    var zero = fraction(0, 1);
    var oldInitialKey = Number(staff.key(zero));
    var newInitialKey = oldInitialKey + preset.keyDelta;
    if (newInitialKey < -7 || newInitialKey > 7) {
        throw new Error("Anfangstonart würde außerhalb des unterstützten Bereichs von -7 bis +7 Vorzeichen liegen.");
    }

    return {
        staffIndex: staffIndex,
        target: preset.target,
        notes: notePlans,
        keys: keyPlans,
        addInitialKeySig: !initialKeySigFound && newInitialKey !== 0,
        initialKey: newInitialKey
    };
}

function signatureForProtectedStaff(score, staffIndex) {
    var parts = [];
    var staff = score.staves[staffIndex];
    var seg = score.firstSegment();
    while (seg) {
        parts.push("K" + Number(staff.key(seg.fraction)));
        for (var v = 0; v < 4; ++v) {
            var el = seg.elementAt(staffIndex * TRACKS_PER_STAFF + v);
            if (el && el.type === Element.CHORD) appendChordSignature(el, parts);
        }
        seg = seg.next;
    }
    return parts.join(";");
}

function appendChordSignature(chord, parts) {
    var notes = chord.notes;
    for (var n = 0; n < listLength(notes); ++n) parts.push("N" + Number(notes[n].pitch) + "/" + Number(notes[n].tpc1) + "/" + Number(notes[n].tpc2));
    var graces = chord.graceNotes;
    for (var g = 0; g < listLength(graces); ++g) appendChordSignature(graces[g], parts);
}

function addInitialKeySig(score, plan) {
    var cursor = score.newCursor();
    cursor.track = plan.staffIndex * TRACKS_PER_STAFF;
    cursor.rewind(0);
    var ks = newElement(Element.KEYSIG);
    ks.actualKey = plan.initialKey;
    ks.concertKey = plan.initialKey;
    cursor.add(ks);
}

function applyStaffPlan(score, plan) {
    for (var i = 0; i < plan.notes.length; ++i) {
        var p = plan.notes[i];
        p.note.pitch = p.pitch;
        p.note.tpc1 = p.tpc1;
        p.note.tpc2 = p.tpc2;
    }
    for (var k = 0; k < plan.keys.length; ++k) {
        var kp = plan.keys[k];
        kp.element.actualKey = kp.actualKey;
        kp.element.concertKey = kp.concertKey;
    }
    if (plan.addInitialKeySig) addInitialKeySig(score, plan);
}

function runTargets(trebleTarget, bassTarget) {
    var score = curScore;
    if (!score) throw new Error("Kein Score geöffnet.");

    var report = buildBaseReport(score, trebleTarget, bassTarget);
    report.transposition = {
        targets: { treble: trebleTarget, bass: bassTarget },
        changedStaffs: [],
        changedTrebleStaffs: [],
        changedBassStaffs: [],
        skippedStaffs: [],
        changedNotes: 0,
        changedKeySignatures: 0,
        insertedInitialKeySignatures: 0
    };

    var plans = [];
    var selected = {};
    for (var i = 0; i < report.staves.length; ++i) {
        var si = report.staves[i];
        if (si.clefValues.length !== 1 || (si.clefValue !== ClefType.G && si.clefValue !== ClefType.F)) continue;
        var desired = si.clefValue === ClefType.G ? trebleTarget : bassTarget;
        if (desired === "C") continue;
        try {
            var p = collectStaffPlan(score, i, targetPreset(desired));
            plans.push(p);
            selected[i] = true;
        } catch (e) {
            si.decision = "NICHT TRANSPONIEREN";
            si.reason = asString(e);
            report.transposition.skippedStaffs.push(i + 1);
            report.warnings.push("Staff " + (i + 1) + " wurde aus Sicherheitsgründen übersprungen: " + asString(e));
        }
    }

    var protectedBefore = {};
    for (var s = 0; s < score.nstaves; ++s) if (!selected[s]) protectedBefore[s] = signatureForProtectedStaff(score, s);

    if (plans.length > 0) {
        score.startCmd("Ensemble Transposer G=" + trebleTarget + " F=" + bassTarget);
        var rollbackReason = "";
        try {
            for (var pi = 0; pi < plans.length; ++pi) applyStaffPlan(score, plans[pi]);
            for (var ps in protectedBefore) {
                if (protectedBefore.hasOwnProperty(ps)) {
                    var idx = Number(ps);
                    if (signatureForProtectedStaff(score, idx) !== protectedBefore[ps]) {
                        rollbackReason = "Ein nicht ausgewählter Staff wurde unerwartet verändert (Staff " + (idx + 1) + ").";
                        break;
                    }
                }
            }
        } catch (e2) {
            rollbackReason = asString(e2);
        }
        if (rollbackReason.length > 0) {
            score.endCmd(true);
            report.status = "FEHLER";
            report.warnings.push("Transposition vollständig zurückgerollt: " + rollbackReason);
            writeReport(score, report);
            return;
        }
        score.endCmd(false);
    }

    for (var x = 0; x < plans.length; ++x) {
        var pl = plans[x];
        var clefValue = report.staves[pl.staffIndex].clefValue;
        report.transposition.changedStaffs.push(pl.staffIndex + 1);
        if (clefValue === ClefType.G) report.transposition.changedTrebleStaffs.push(pl.staffIndex + 1);
        if (clefValue === ClefType.F) report.transposition.changedBassStaffs.push(pl.staffIndex + 1);
        report.transposition.changedNotes += pl.notes.length;
        report.transposition.changedKeySignatures += pl.keys.length;
        if (pl.addInitialKeySig) report.transposition.insertedInitialKeySignatures += 1;
    }

    if (plans.length === 0 && (trebleTarget !== "C" || bassTarget !== "C")) {
        report.warnings.push("Keine sicher transponierbaren Staves für die gewählten Ziele gefunden.");
    }
    report.status = report.warnings.length ? "WARNUNG" : "OK";
    writeReport(score, report);
}

function analyze() {
    var score = curScore;
    if (!score) throw new Error("Kein Score geöffnet.");
    var report = buildBaseReport(score, "C", "C");
    report.target = "ANALYZE";
    writeReport(score, report);
}

// Backwards-compatible actions from the CLI version.
function targetC() { runTargets("C", "C"); }
function targetBb() { runTargets("Bb", "C"); }
function targetEb() { runTargets("Eb", "C"); }

// Hosted/web combinations.
function targetC_Bb() { runTargets("C", "Bb"); }
function targetC_Eb() { runTargets("C", "Eb"); }
function targetBb_Bb() { runTargets("Bb", "Bb"); }
function targetBb_Eb() { runTargets("Bb", "Eb"); }
function targetEb_Bb() { runTargets("Eb", "Bb"); }
function targetEb_Eb() { runTargets("Eb", "Eb"); }
