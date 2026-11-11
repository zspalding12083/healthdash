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


if __name__ == '__main__':
    unittest.main()
