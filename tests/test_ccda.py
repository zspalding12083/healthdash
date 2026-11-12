"""Health summary (C-CDA) import. A made-up document only."""

import io
import os
import tempfile
import time
import unittest
import zipfile

from healthdash import ccda
from healthdash.store import Store
from healthdash.uploads import Uploads

DOC = b"""<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <title>Summary of Care</title><effectiveTime value="20200304120000-0500"/>
 <custodian><assignedCustodian><representedCustodianOrganization><name>Sample Health Network</name></representedCustodianOrganization></assignedCustodian></custodian>
 <componentOf><encompassingEncounter><id extension="E1"/><code code="99213"><originalText>Office Visit</originalText></code>
  <effectiveTime><low value="20200304100000-0500"/></effectiveTime>
  <responsibleParty><assignedEntity><representedOrganization><name>Sample Health Network</name></representedOrganization></assignedEntity></responsibleParty>
  <location><healthCareFacility><location><name>Riverside Family Practice</name></location></healthCareFacility></location>
 </encompassingEncounter></componentOf>
 <component><structuredBody>
  <component><section><code code="29299-5"/><title>Reason for Visit</title><text><table><tr><td>Diabetes follow-up</td></tr><tr><td>Cough</td></tr></table></text></section></component>
  <component><section><code code="30954-2"/><title>Results</title><text><content ID="r1">GLUCOSE</content></text>
   <entry><organizer><code><originalText>BASIC METABOLIC PANEL</originalText></code><effectiveTime><low value="20200304"/></effectiveTime>
    <component><observation><code><originalText><reference value="#r1"/></originalText></code><effectiveTime value="20200304"/>
     <value xsi:type="PQ" value="130" unit="mg/dL"/><interpretationCode code="H"/>
     <referenceRange><observationRange><value xsi:type="IVL_PQ"><low value="70"/><high value="99"/></value></observationRange></referenceRange></observation></component>
    <component><observation><code><originalText>units</originalText></code><value xsi:type="ST">mg/dL</value></observation></component>
   </organizer></entry></section></component>
  <component><section><code code="10160-0"/><title>Medications</title><text><content ID="m1">Examplol 10 MG Oral Tab</content><content ID="s1">Take 1 tablet daily</content><content ID="m2">WATER</content></text>
   <entry><substanceAdministration><text><reference value="#s1"/></text><statusCode code="active"/><effectiveTime xsi:type="IVL_TS"><low value="20190101"/></effectiveTime>
    <doseQuantity value="10" unit="mg"/><consumable><manufacturedProduct><manufacturedMaterial><code><originalText><reference value="#m1"/></originalText></code></manufacturedMaterial></manufacturedProduct></consumable></substanceAdministration></entry>
   <entry><substanceAdministration><statusCode code="completed"/><consumable><manufacturedProduct><manufacturedMaterial><code><originalText><reference value="#m2"/></originalText></code></manufacturedMaterial></manufacturedProduct></consumable></substanceAdministration></entry>
  </section></component>
  <component><section><code code="11450-4"/><title>Active Problems</title><text><content ID="p1">Prediabetes</content></text>
   <entry><act><statusCode code="active"/><entryRelationship><observation><effectiveTime><low value="20180505"/></effectiveTime>
    <value xsi:type="CD"><originalText><reference value="#p1"/></originalText></value></observation></entryRelationship></act></entry></section></component>
  <component><section><code code="48765-2"/><title>Allergies</title><text><content ID="a1">Sampleillin</content></text>
   <entry><act><entryRelationship><observation><participant><participantRole><playingEntity><code><originalText><reference value="#a1"/></originalText></code></playingEntity></participantRole></participant>
    <entryRelationship><observation><value xsi:type="CD" displayName="Hives"/></observation></entryRelationship></observation></entryRelationship></act></entry></section></component>
 </structuredBody></component>
</ClinicalDocument>"""


class CcdaTests(unittest.TestCase):
    def test_parse(self):
        d = ccda.parse(DOC)
        self.assertEqual((d['visit']['date'], d['visit']['kind'], d['visit']['location']),
                         ('2020-03-04', 'Office Visit', 'Riverside Family Practice'))
        self.assertEqual(d['visit']['reason'], 'Diabetes follow-up; Cough')
        self.assertEqual([(l['analyte'], l['num'], l['unit'], l['ref_range'], l['flag']) for l in d['labs']],
                         [('GLUCOSE', 130.0, 'mg/dL', '70-99', 'H')])           # "units" row skipped
        self.assertEqual([(m['name'], m['dose'], m['schedule'], m['start_date']) for m in d['medications']],
                         [('Examplol 10 MG Oral Tab', '10 mg', 'Take 1 tablet daily', '2019-01-01')])  # no WATER
        self.assertEqual((d['problems'][0]['name'], d['problems'][0]['first_noted']), ('Prediabetes', '2018-05-05'))
        self.assertEqual((d['allergies'][0]['substance'], d['allergies'][0]['reaction']), ('Sampleillin', 'Hives'))
        self.assertIn('Diabetes follow-up', ccda.page(d))

    def test_placeholder_1900_date_makes_no_visit(self):
        doc = DOC.replace(b'<low value="20200304100000-0500"/>',
                          b'<low value="19000101120000-0500"/><high value="19000101120000-0500"/>')
        d = ccda.parse(doc)
        self.assertIsNone(d['visit'])
        self.assertEqual(d['doc_date'], '2020-03-04')                   # dated when it was made
        self.assertEqual(d['medications'][0]['start_date'], '2019-01-01')   # real dates kept
        self.assertIsNone(ccda._date('19000101'))

    def test_old_1900_visit_is_cleaned_up(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = Store(os.path.join(tmp.name, 'h.db'))
        self.addCleanup(s.close)
        s.upsert_documents([{'id': 'u1', 'path': 'Uploaded/export.xml', 'ext': '.xml', 'doc_date': '1900-01-01',
                             'needs_review': False},
                            {'id': 'p1', 'path': 'Imported/x/page.html', 'doc_date': '1900-01-01', 'needs_review': False}])
        s.upsert_imported('encounters', [{'id': 'ccda-visit-1', 'date': '1900-01-01', 'docs': ['p1']},
                                         {'id': 'ccda-visit-2', 'date': '2020-01-01', 'docs': ['p1']}])
        self.assertEqual(s.placeholder_dates(), (1, ['u1']))
        self.assertEqual([e['id'] for e in s.list('encounters')], ['ccda-visit-2'])
        self.assertIsNone(s.document('p1')['doc_date'])
        self.assertEqual(s.placeholder_dates(), (0, []))     # once only

    def test_cerner_style_procedures_and_links_to_existing_entries(self):
        doc = DOC.replace(b'<code code="99213"><originalText>Office Visit</originalText></code>', b'')
        doc = doc.replace(b'<effectiveTime><low value="20200304100000-0500"/></effectiveTime>',
                          b'<effectiveTime><low value="20200304100000-0500"/><high value="20200307120000-0500"/></effectiveTime>')
        doc = doc.replace(b'</structuredBody>', b"""<component><section><code code="46240-8"/><title>Encounter</title>
          <entry><encounter><code nullFlavor="UNK"><originalText>Inpatient</originalText></code>
          <performer><assignedEntity><assignedPerson><name><given>Robin</given><family>Example</family><suffix>MD</suffix></name></assignedPerson></assignedEntity></performer></encounter></entry></section></component>
          <component><section><code code="47519-4"/><title>Procedures</title><text><table><tbody>
          <tr><td><content ID="pr1">Knee arthroscopy (None) 1</content></td><td>3/5/20</td></tr>
          <tr><td><content ID="pr2">TONSILLECTOMY 6/1998</content></td><td/></tr></tbody></table></text>
          <entry><procedure><code nullFlavor="UNK"><originalText><reference value="#pr1"/></originalText></code><statusCode code="completed"/></procedure></entry>
          <entry><procedure><code nullFlavor="UNK"><originalText><reference value="#pr2"/></originalText></code><statusCode code="completed"/></procedure></entry>
          </section></component></structuredBody>""")
        d = ccda.parse(doc)
        self.assertEqual((d['visit']['kind'], d['visit']['provider'], d['visit']['end_date']), ('Inpatient', 'Robin Example MD', '2020-03-07'))
        self.assertEqual([(p['name'], p['date']) for p in d['procedures']],
                         [('Knee arthroscopy', '2020-03-05'), ('TONSILLECTOMY', '1998-06')])
        with tempfile.TemporaryDirectory() as tmp:
            files = os.path.join(tmp, 'f'); os.makedirs(files)
            store = Store(os.path.join(tmp, 't.db'))
            store.upsert_imported('encounters', [{'id': 'enc-old', 'date': '2020-03-04', 'type': 'Inpatient', 'reason': 'Knee'}])
            store.upsert_imported('procedures', [{'id': 'proc-old', 'date': '1998-06', 'name': 'Tonsillectomy'}])
            up = Uploads(store, files, recheck=0)
            sha = up.add('summary.xml', doc)['id']
            for _ in range(200):
                if store.document(sha)['status'] != 'processing':
                    break
                time.sleep(0.05)
            self.assertEqual(store.document(sha)['status'], 'done')
            self.assertEqual([e['id'] for e in store.list('encounters')], ['enc-old'])      # linked, not doubled
            page = [x for x in store.documents() if x['path'].startswith('Imported/')]
            self.assertEqual([(x['title'], x['category']) for x in page], [('Hospital stay', 'hospital')])
            self.assertIn(page[0]['id'], store.get('encounters', 'enc-old')['docs'])
            self.assertIn(page[0]['id'], store.get('procedures', 'proc-old')['docs'])        # 1998-06 matched
            self.assertEqual(sorted(p['name'] for p in store.list('procedures')), ['Knee arthroscopy', 'Tonsillectomy'])
            self.assertNotEqual(page[0]['id'], sha)    # the uploaded file keeps its own entry
            store.close()

    def test_medication_lines_merge_into_periods(self):
        from healthdash import ccda as C
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            rows = [
                {"id": "ccda-med-a", "name": "Examplol 10 MG Oral Tablet", "start_date": "2020-01-01", "end_date": "2020-03-01", "status": "stopped"},
                {"id": "ccda-med-b", "name": "EXAMPLOL HCL - 20 MG Oral Tab", "start_date": "2020-03-20", "status": "active"},
                {"id": "ccda-med-c", "name": "Examplol 10 MG Oral Tablet", "start_date": "2023-05-01", "end_date": "2023-06-01", "status": "stopped"},
                {"id": "ccda-med-d", "name": "Fakeazole 500 MG", "start_date": "2021-01-01", "end_date": "2021-01-08", "status": "stopped"},
                {"id": "hand-picked", "name": "Examplol", "start_date": "2010-01-01", "status": "active"},
            ]
            store.upsert_imported("medications", rows)
            r = store.consolidate_meds(C.drug_key, C.record_ids)
            self.assertEqual(r, {"lines": 4, "entries": 3})
            meds = {(m["name"], m["start_date"]): m for m in store.list("medications")}
            cur = meds[("EXAMPLOL HCL - 20 MG Oral Tab", "2020-01-01")]       # 2020 lines are one period
            self.assertEqual((cur["status"], cur["end_date"]), ("active", None))
            self.assertIn("Listed 2 times", cur["notes"])
            self.assertEqual(meds[("Examplol 10 MG Oral Tablet", "2023-05-01")]["end_date"], "2023-06-01")
            self.assertIn(("Examplol", "2010-01-01"), meds)                    # a hand-entered one is left alone
            self.assertEqual(store.consolidate_meds(C.drug_key, C.record_ids), {"lines": 3, "entries": 3})  # stable
            store.close()

    def test_over_the_counter_meds_have_no_start_or_stop(self):
        from healthdash import ccda as C
        from healthdash.store import is_otc
        self.assertTrue(is_otc("Tylenol CAPS"))
        self.assertTrue(is_otc("Loratadine (CLARITIN) 10 MG Oral Tab"))
        self.assertFalse(is_otc("Oxycodone-Acetaminophen 5-325 MG Oral Tablet"))
        self.assertFalse(is_otc("naproxen 500 mg oral tablet"))
        self.assertFalse(is_otc("Examplol 10 MG Oral Tablet"))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            store.upsert_imported("medications", [
                {"id": "ccda-med-a", "name": "Advil TABS", "start_date": "2019-01-01", "end_date": "2019-02-01", "status": "stopped"},
                {"id": "ccda-med-b", "name": "Advil TABS", "start_date": "2024-06-01", "status": "active", "needs_review": True},
                {"id": "ccda-med-c", "name": "Examplol 10 MG Oral Tablet", "start_date": "2020-01-01", "status": "active"},
            ])
            self.assertEqual(store.consolidate_meds(C.drug_key, C.record_ids), {"lines": 3, "entries": 2})  # years apart, still one
            otc = [m for m in store.list("medications") if m["otc"]]
            store.set_otc({"words": ["Samplocet"], "not": [r"samplocet\s*900"]})        # more names, kept private
            self.assertTrue(store.is_otc("Samplocet 300 mg") and not store.is_otc("Samplocet 900 mg"))
            self.assertEqual(len(otc), 1)
            titles = [e["title"] for e in store.timeline() if e["kind"] == "medications"]
            self.assertEqual(titles, ["Started Examplol 10 MG Oral Tablet"])
            self.assertEqual([m["name"] for m in store.summary()["current_meds"]], ["Examplol 10 MG Oral Tablet"])
            store.close()

    def test_current_medication_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            store.upsert_imported("medications", [
                {"id": "ccda-med-a", "name": "Examplol 10 MG Oral Tablet", "dose": "10.0 mg", "start_date": "2020-01-01", "status": "active", "docs": ["d1", "d2"]},
                {"id": "ccda-med-b", "name": "Brandol XL", "dose": "10 mg", "start_date": "2022-01-01", "status": "active", "docs": ["d3"]},
                {"id": "ccda-med-c", "name": "Fakeazole 500 MG", "start_date": "2021-01-01", "status": "active"},
                {"id": "ccda-med-d", "name": "Samplorin 20 MG", "start_date": "2019-01-01", "end_date": "2019-05-01", "status": "stopped"},
                {"id": "ccda-med-e", "name": "Tylenol CAPS", "start_date": "2018-01-01", "status": "active"},
            ])
            store.db.execute("CREATE TABLE IF NOT EXISTS drug_alias (brand TEXT PRIMARY KEY, generic TEXT)")
            store.db.execute("INSERT INTO drug_alias VALUES ('brandol', 'examplol')")
            text = "Examplol 10MG\nSamplorn 20mg\nNewdrug 5 mg\n"
            plan = store.apply_med_list(text, "2026-01-15")
            self.assertEqual([k["id"] for k in plan["keep"]], ["ccda-med-a"])
            self.assertEqual([m["id"] for m in plan["merge"]], ["ccda-med-b"])        # the same drug under its brand
            self.assertEqual([x["id"] for x in plan["stop"]], ["ccda-med-c"])         # over-the-counter Tylenol is left alone
            self.assertEqual([a["name"] for a in plan["add"]], ["Samplorin", "Newdrug"])   # stopped in 2019: a new period
            self.assertEqual(store.get("medications", "ccda-med-a")["status"], "active")   # nothing saved yet
            store.apply_med_list(text, "2026-01-15", apply=True)
            a = store.get("medications", "ccda-med-a")
            self.assertEqual((a["dose"], a["docs"]), ("10 mg", ["d1", "d2", "d3"]))
            self.assertNotIn("Earlier records", a["notes"])                            # 10.0 mg is 10 mg
            c = store.get("medications", "ccda-med-c")
            self.assertEqual((c["status"], c["end_date"]), ("stopped", "2026-01-15"))
            self.assertIsNone(store.get("medications", "ccda-med-b"))
            self.assertEqual(len(store.summary()["current_meds"]), 3)
            again = store.apply_med_list(text, "2026-01-15")
            self.assertEqual((len(again["stop"]), len(again["add"]), len(again["merge"])), (0, 0, 0))
            with self.assertRaises(Exception):
                store.apply_med_list("", "2026-01-15")
            store.close()

    def test_people_on_a_provider_line(self):
        from healthdash.ccda import people, doctor_key
        self.assertEqual(people("Dr. Pat Example (attending); ER: Dr. Sam Sample"), ["Dr. Pat Example", "Dr. Sam Sample"])
        self.assertEqual(doctor_key("Dr. Pat Example (cardiology)"), doctor_key("Pat Q. Example MD"))
        self.assertEqual(doctor_key("Referring: Lee Demo, RPA-C"), "lee demo")

    def test_duplicates_merge(self):
        from healthdash.uploads import refresh_doctors
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            store.upsert_imported("providers", [{"id": "doc-pat", "name": "Dr. Pat Example", "specialty": "Cardiology"},
                                                {"id": "ccda-doctor-old", "name": "Dr. Pat Example (cardiology)"}])
            store.upsert_imported("encounters", [
                {"id": "e1", "date": "2020-01-01", "provider": "Dr. Pat Example (cardiology)", "facility": "EXAMPLE GENERAL", "docs": ["d1"]},
                {"id": "e2", "date": "2020-02-01", "provider": "Pat Q. Example MD; ER: Sam Sample DO", "facility": "Example General", "docs": ["d2"]},
                {"id": "e3", "date": "2020-03-01", "provider": "Sam Sample DO", "facility": "EGH / EGH", "docs": ["d3"]},
                {"id": "e4", "date": "2020-04-01", "facility": "Example General", "docs": []},
            ])
            store.upsert_imported("procedures", [
                {"id": "proc-1", "date": "2019-05-02", "name": "Left knee arthroscopy with meniscus repair", "docs": ["d4"]},
                {"id": "ccda-proc-a", "date": "2019-05", "name": "Arthroscopy Knee (Left)", "docs": ["d5"]},
                {"id": "ccda-proc-b", "date": "2019-05-02", "name": "Collection of venous blood", "docs": ["d6"]},
            ])
            store.set_place_names({"Example General Hospital": ["EGH"]})
            store.tidy_places()
            self.assertEqual(store.places()[0], "Example General")                  # most used first
            self.assertEqual({e["facility"] for e in store.list("encounters")}, {"Example General", "Example General Hospital"})
            self.assertEqual(store.merge_imported_procedures(), 1)            # the blood draw isn't the same procedure
            self.assertEqual(store.get("procedures", "proc-1")["docs"], ["d4", "d5"])
            self.assertIsNone(store.get("procedures", "ccda-proc-a"))
            refresh_doctors(store)
            names = sorted(p["name"] for p in store.list("providers"))
            self.assertEqual(names, ["Dr. Pat Example", "Sam Sample DO"])
            self.assertEqual(sorted(store.get("providers", "doc-pat")["docs"]), ["d1", "d2"])
            store.close()

    def test_sources_and_conditions_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            store.upsert_imported("medications", [
                {"id": "med-examplol", "name": "Examplol (Brandol)", "start_date": "2020-01-01", "status": "active", "docs": ["d1"]},
                {"id": "ccda-med-a", "name": "Examplol 10 MG Oral Tablet", "start_date": "2021-03-01", "status": "active", "docs": ["d2"]},
                {"id": "ccda-med-b", "name": "Examplol 10 MG Oral Tablet", "start_date": "2010-01-01", "end_date": "2010-02-01", "status": "stopped"},
                {"id": "ccda-med-c", "name": "Fakeazole 500 MG", "start_date": "2021-01-01", "status": "active"},
            ])
            self.assertEqual(store.merge_med_sources(), 1)          # the 2010 course is another period
            store.upsert_imported("medications", [
                {"id": "med-combo", "name": "Samplocodone-acetaminophen (Brandocet)", "start_date": "2020-03-02", "end_date": "2020-03-04", "status": "stopped"},
                {"id": "ccda-med-d", "name": "acetaminophen-samplocodone (Brandocet 5/325)", "start_date": "2020-03-02", "end_date": "2020-05-01", "status": "stopped"}])
            self.assertEqual(store.merge_med_sources(), 1)          # the same combination, either order
            self.assertEqual(store.get("medications", "med-examplol")["docs"], ["d1", "d2"])
            self.assertIsNone(store.get("medications", "ccda-med-a"))
            store.upsert_imported("conditions", [
                {"id": "ccda-cond-1", "name": "Sample syndrome(Confirmed)", "status": "resolved", "first_noted": "2015-04-01"},
                {"id": "ccda-cond-2", "name": "Sample syndrome (Problem)", "status": "active", "first_noted": "1901-01-01"},
                {"id": "ccda-cond-3", "name": "Sample syndrome 2", "status": "active", "docs": ["d3"]},
                {"id": "ccda-cond-4", "name": "Hx of demo fracture", "status": "resolved"},
                {"id": "cond-demo", "name": "History of demo fracture", "status": "resolved", "first_noted": "2001"},
            ])
            self.assertEqual(store.merge_conditions(), 3)
            names = {c["name"]: c for c in store.list("conditions")}
            self.assertEqual(sorted(names), ["History of demo fracture", "Sample syndrome"])
            self.assertEqual((names["Sample syndrome"]["status"], names["Sample syndrome"]["first_noted"]), ("active", "2015-04-01"))
            store.close()

    def test_imaging_reports_in_a_summary(self):
        from healthdash.uploads import imaging_from_summaries
        text = ("Radiology Results ******** Order: XR Wrist LEFT Exam Completion Date:3/4/2024 10:05 Result Status : completed "
                "FINDINGS: Normal. IMPRESSION: No fracture. Electronically Signed by: Pat Example Signature Date:3/4/2024 "
                "Order: CT Head w/o IV contrast Exam Completion Date:03/04/2024 11:00 Result Status : completed IMPRESSION: Normal CT head. "
                "******** Laboratory Results ******** Order: CBC")
        self.assertEqual([(r["name"], r["date"], r["result"], r["provider"]) for r in ccda.radiology_reports(text)],
                         [("XR Wrist LEFT", "2024-03-04", "No fracture.", "Pat Example"),
                          ("CT Head w/o IV contrast", "2024-03-04", "Normal CT head.", None)])
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            store.upsert_documents([{"id": "a" * 64, "path": "Imported/x.html", "title": "Hospital stay", "ext": ".html",
                                     "facility": "Example General", "text": text, "needs_review": False}])
            store.upsert_imported("tests", [{"id": "test-ct", "date": "2024-03-04", "category": "imaging", "name": "CT head without contrast"}])
            imaging_from_summaries(store)
            names = sorted(t["name"] for t in store.list("tests"))
            self.assertEqual(names, ["CT head without contrast", "XR Wrist LEFT"])      # the CT joins the detailed entry
            self.assertEqual(store.get("tests", "test-ct")["docs"], ["a" * 64])
            store.close()

    def test_drugs_given_during_a_procedure_are_set_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            store.upsert_imported("procedures", [{"id": "p1", "date": "2020-05-02", "name": "Knee arthroscopy"}])
            store.upsert_imported("medications", [
                {"id": "ccda-med-a", "name": "samplazolam (Sampled (ANES)) 2 mg/2 mL", "start_date": "2019-01-01", "end_date": "2019-01-01", "status": "stopped"},
                {"id": "ccda-med-b", "name": "Examplol 10 MG Oral Tablet", "start_date": "2020-05-02", "end_date": "2020-05-02", "status": "stopped"},
                {"id": "ccda-med-c", "name": "Fakeazole 500 MG Oral Tab", "start_date": "2021-01-01", "end_date": "2021-01-08", "status": "stopped"},
                {"id": "med-hand", "name": "Demolin Inj", "start_date": "2022-01-01", "end_date": "2022-01-01", "status": "stopped"},
            ])
            given = {m["id"] for m in store.list("medications") if m["given"]}
            self.assertEqual(given, {"ccda-med-a", "ccda-med-b"})   # anesthesia; a one-day line on the day of a procedure
            self.assertFalse(any(e["id"] in given for e in store.timeline()))
            store.close()

    def test_refuses_entities(self):
        with self.assertRaises(ccda.NotCCDA):
            ccda.parse(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')

    def test_zip_import_adds_once_and_dedupes_results(self):
        later = DOC.replace(b'20200304100000', b'20200601100000').replace(b'E1', b'E2') \
                   .replace(b'<content ID="m1">Examplol', b'<content ID="m9">Other').replace(b'#m1', b'#m9')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('IHE_XDM/P1/DOC0001.XML', DOC)
            z.writestr('IHE_XDM/P1/DOC0002.XML', later)
            z.writestr('IHE_XDM/P1/METADATA.XML', b'<x/>')
        with tempfile.TemporaryDirectory() as tmp:
            files = os.path.join(tmp, 'f'); os.makedirs(files)
            store = Store(os.path.join(tmp, 't.db'))
            store.upsert_imported('labs', [{'id': 'old', 'date': '2020-03-04', 'analyte': 'Glucose', 'value': '130 mg/dL', 'num': 130}])
            up = Uploads(store, files, recheck=0)
            sha = up.add('HealthSummary.zip', buf.getvalue())['id']
            for _ in range(200):
                if store.document(sha)['status'] != 'processing':
                    break
                time.sleep(0.05)
            d = store.document(sha)
            self.assertEqual((d['status'], d['needs_review']), ('done', 0), d['review_note'])
            self.assertEqual(len(store.list('encounters')), 2)
            self.assertEqual(len([l for l in store.list('labs') if l['analyte'] == 'Glucose']), 1)  # already had it
            meds = {m['name']: m for m in store.list('medications')}
            self.assertEqual(meds['Examplol 10 MG Oral Tab']['status'], 'stopped')   # gone from the later list
            self.assertEqual(meds['Examplol 10 MG Oral Tab']['end_date'], '2020-06-01')
            self.assertEqual(meds['Other 10 MG Oral Tab']['status'], 'active')
            self.assertTrue(up.add('again.zip', buf.getvalue())['duplicate'])
            store.close()



class DoctorTests(unittest.TestCase):
    def test_names_and_places(self):
        self.assertEqual(ccda.doctor_key('Pat Q. Example MD'), ccda.doctor_key('Pat Example'))
        self.assertEqual(ccda.doctor_key('Lee R. Sample PA-C'), 'lee sample')
        self.assertIsNone(ccda.doctor_key('MD ER'))
        self.assertEqual(ccda.place('Riverside Clinic', 'Riverside Clinic'), 'Riverside Clinic')
        self.assertEqual(ccda.place('Lab', None, 'Sample Hospital'), 'Lab / Sample Hospital')

    def test_doctors_come_from_imported_visits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, 't.db'))
            store.upsert_imported('encounters', [
                {'id': 'ccda-visit-1', 'date': '2020-01-02', 'provider': 'Pat Example', 'facility': 'Riverside Clinic / Riverside Clinic', 'docs': ['d1']},
                {'id': 'ccda-visit-2', 'date': '2021-05-06', 'provider': 'Pat Q. Example MD', 'facility': 'Riverside Clinic', 'docs': ['d2']},
                {'id': 'ccda-visit-3', 'date': '2021-05-07', 'provider': 'MD ER', 'facility': 'Sample Hospital'},
                {'id': 'ccda-visit-4', 'date': '2022-01-01', 'provider': 'Lee Sample', 'facility': 'Sample Hospital'}])
            store.create('providers', {'name': 'Dr. Lee Sample', 'specialty': 'Cardiology'})
            up = Uploads(store, os.path.join(tmp, 'f'), recheck=0)
            rows = up.doctors()
            self.assertEqual([r['name'] for r in rows], ['Pat Q. Example MD'])   # Lee Sample was entered by hand
            self.assertEqual(rows[0]['facility'], 'Riverside Clinic')
            self.assertIn('2 visits, 2020-01-02 to 2021-05-06', rows[0]['notes'])
            self.assertEqual(sorted(rows[0]['docs']), ['d1', 'd2'])
            store.tidy_imported_places(ccda.place)
            self.assertEqual(store.get('encounters', 'ccda-visit-1')['facility'], 'Riverside Clinic')
            store.close()


class MergeOrderTests(unittest.TestCase):
    """The same records imported in either order end up the same. Made-up data."""

    def run_lines(self, order):
        import tempfile
        from healthdash.store import Store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = Store(os.path.join(tmp.name, 'h.db'))
        self.addCleanup(s.close)
        s.upsert_documents([{'id': 'd1', 'path': 'a', 'doc_date': '2024-06-01', 'needs_review': False},
                            {'id': 'd2', 'path': 'b', 'doc_date': '2025-03-16', 'needs_review': False},
                            {'id': 'd3', 'path': 'c', 'doc_date': '2025-03-24', 'needs_review': False}])
        lines = {
            'early': {'id': 'ccda-med-1', 'docs': ['d1'], 'name': 'Examplitide (Fakeza 2 mg)', 'dose': '2 mg',
                      'start_date': '2024-02-14', 'status': 'active'},
            'list': {'id': 'ccda-med-2', 'docs': ['d2'], 'name': 'examplitide (Fakeza 2 mg)', 'dose': None,
                     'start_date': '2024-02-14', 'status': 'active'},
            'stop': {'id': 'ccda-med-3', 'docs': ['d3'], 'name': 'Fakeza', 'dose': '2 mg',
                     'start_date': '2024-05-01', 'end_date': '2025-03-24', 'status': 'stopped'},
        }
        for k in order:
            s.upsert_imported('medications', [lines[k]])
            s.consolidate_meds(ccda.drug_key, ccda.record_ids)
        once = [{f: v for f, v in m.items() if f != 'updated'} for m in s.list('medications')]
        for _ in range(2):   # running it again changes nothing, though the brand line is merged away
            s.consolidate_meds(ccda.drug_key, ccda.record_ids)
        self.assertEqual([{f: v for f, v in m.items() if f != 'updated'} for m in s.list('medications')], once)
        return [(m['start_date'], m['end_date'], m['status'], m['dose']) for m in s.list('medications')]

    def test_placeholder_words_are_not_drugs(self):
        import tempfile
        from healthdash.store import Store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = Store(os.path.join(tmp.name, 'h.db'))
        self.addCleanup(s.close)
        s.upsert_imported('medications', [
            {'id': 'ccda-med-1', 'docs': [], 'name': 'Misc Med (EXAMPLAMATE 100MG TABLETS)', 'start_date': '2020-01-01', 'status': 'active'},
            {'id': 'ccda-med-2', 'docs': [], 'name': 'examplamate 200 MG Oral Tablet', 'start_date': '2020-02-01', 'status': 'active'},
            {'id': 'ccda-med-3', 'docs': [], 'name': 'Misc Med (FAKEPRIL 5MG)', 'start_date': '2020-01-01', 'status': 'active'},
            {'id': 'ccda-med-4', 'docs': [], 'name': 'sampledol (Brandol) 5 MG', 'start_date': '2020-01-01', 'status': 'active'},
            {'id': 'ccda-med-5', 'docs': [], 'name': 'Brandol 5 MG', 'start_date': '2020-02-01', 'status': 'active'},
            {'id': 'ccda-med-6', 'docs': [], 'name': 'Brandol (sampledol 5 mg)', 'start_date': '2020-03-01', 'status': 'active'}])
        s.consolidate_meds(ccda.drug_key, ccda.record_ids)
        names = sorted(m['name'] for m in s.list('medications'))
        self.assertEqual(len(names), 3, names)    # examplamate, fakepril, sampledol: misc joins nothing
        self.assertNotIn('misc', {b for b, g in s.db.execute('SELECT * FROM drug_alias')} | {g for b, g in s.db.execute('SELECT * FROM drug_alias')})

    def test_medication_period_is_the_same_in_any_order(self):
        want = [('2024-02-14', '2025-03-24', 'stopped', '2 mg')]
        self.assertEqual(self.run_lines(['early', 'list', 'stop']), want)
        self.assertEqual(self.run_lines(['stop', 'list', 'early']), want)
        self.assertEqual(self.run_lines(['list', 'stop', 'early']), want)


if __name__ == '__main__':
    unittest.main()
