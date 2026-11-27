"""Reading web pages and MyChart result pages. Made-up data only."""

import unittest

from healthdash.extract import html_text

PAGE = b"""<!doctype html><html><head><title>Test Results</title><script>alert(1)</script>
<style>.x{color:red}</style></head><body onload="steal()"><nav>Home Messages Billing</nav>
<h1>Lab Results</h1><p>Riverside General Hospital Laboratory</p>
<table><tr><th>Test</th><th>Result</th></tr><tr><td>Specimen collected</td><td>03/04/2020</td></tr></table>
<pre>Final Result  Collected 04-Mar-2020 07:46
GLUCOSE          130 H    74-106     mg/dL
SODIUM           139      136-145    mmol/L
POTASSIUM        4.1      3.5-5.1    mmol/L
CREATININE       0.9      0.6-1.2    mg/dL</pre>
<p>""" + b"Your results are shown above. " * 12 + b"""</p></body></html>"""


MYCHART = b"""<html><body><img alt="Sample Family Health Center"><h1>GLUCOSE</h1>
<p>Collected on Mar 04, 2020 5:23 PM</p><div>GLUCOSE</div><div>View trends</div>
<div>Normal range: 70 - 200 mg/dL</div><div>Your value is 368 mg/dLThis value is AbnormalNormal range: 70 - 200 mg/dL</div>
<div>POTASSIUM</div><div>Normal range: 3.5 - 5.1 mmol/L</div><div>Your value is 3.2 mmol/LThis value is Low</div>
<div>NAME OF MACHINE</div><div>Value</div><div>1</div><div>Your value is 1</div>
<div>GLU HEMOCUE SERIAL#</div><div>Value</div><div>1,747,119,085</div><div>Your value is 1,747,119,085</div>
<div>Resulting lab:</div><div>IN HOUSE</div></body></html>"""


class MyChartTests(unittest.TestCase):
    def test_values_ranges_flags_and_no_machine_details(self):
        from healthdash import parsers
        rows = {r['analyte']: r for r in parsers.mychart_labs(html_text(MYCHART))}
        self.assertEqual(set(rows), {'Glucose', 'Potassium'})
        self.assertEqual((rows['Glucose']['num'], rows['Glucose']['unit'], rows['Glucose']['ref_range'],
                          rows['Glucose']['flag'], rows['Glucose']['date']), (368, 'mg/dL', '70-200', 'H', '2020-03-04'))
        self.assertEqual(rows['Potassium']['flag'], 'L')

    def test_page_is_titled_by_its_test_and_keeps_the_logo_name(self):
        from healthdash import classify
        t = html_text(MYCHART)
        self.assertIn('Sample Family Health Center', t)
        r = classify.classify(t, 'MyChart - GLUCOSE - Test Details.html', ['Sample Family Health Center'])
        self.assertEqual((r['category'], r['title'], r['facility']),
                         ('labs', 'Lab results: Glucose, Potassium', 'Sample Family Health Center'))


class HtmlTextTests(unittest.TestCase):
    def test_scripts_and_styles_dropped_lines_kept(self):
        t = html_text(PAGE)
        self.assertNotIn('alert', t)
        self.assertNotIn('color:red', t)
        self.assertIn('Lab Results', t)
        self.assertIn('GLUCOSE          130 H    74-106', t)   # columns in <pre> kept
        self.assertIn('Specimen collected  03/04/2020', t)    # table cells two spaces apart
        self.assertIn('Home Messages Billing', t)


if __name__ == '__main__':
    unittest.main()
