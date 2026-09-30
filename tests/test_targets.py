import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class TargetIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / 'targets.json').read_text(encoding='utf-8'))
        cls.snapshot = json.loads((ROOT / 'docs/reference/verification_snapshot.json').read_text(encoding='utf-8'))

    def test_correct_identity_and_account_type(self):
        people, orgs = self.config['people'], self.config['organizations']
        self.assertTrue(24 <= len(people) <= 54)
        for entries, expected in [(people, 'User'), (orgs, 'Organization')]:
            logins = [p['login'].lower() for p in entries]
            self.assertEqual(len(logins), len(set(logins)))
            for p in entries:
                self.assertEqual(p['verification']['account_type'], expected)
                self.assertIsInstance(p['verification']['account_id'], int)
        names = {p['login'].lower() for p in people}
        self.assertFalse(names & {o['login'].lower() for o in orgs})
        self.assertTrue({'lh3','joonan30','jmiao24','harrisongzhang','karpathy','tseemann','hamelsmu','stellaathena'} <= names)
        self.assertNotIn('hamel', names)

    def test_public_curated_repositories_have_observed_relationships(self):
        repos = self.config['repositories']
        names = {r['full_name'].lower() for r in repos}
        self.assertEqual(len(repos), len(names))
        self.assertTrue({'google-deepmind/alphagenome','google-deepmind/alphagenome_research','joonan-lab/cwas','harrisongzhang/thevirtualbiotech','jmiao24/paper2agent'} <= names)
        snapshot = {r['full_name']:r for r in self.snapshot['repositories']}
        for r in repos:
            v = r['verification']
            self.assertIs(v['public'], True)
            self.assertEqual(v['repository_id'], snapshot[r['full_name']]['id'])
            self.assertIn('https://api.github.com/repos/'+r['full_name'], v['sources'])
        for p in self.config['people']:
            self.assertTrue(1 <= len(p['repositories']) <= 2)
            for q in p['verification']['projects']:
                self.assertIn(q['repository'].lower(), names)
                self.assertIn(q['relationship'], {'owner','contributor','lab_project'})
                if q['relationship'] == 'contributor': self.assertGreater(q['contributions_observed'], 0)

    def test_employment_is_not_inferred_from_project_contribution(self):
        people = {p['login'].lower():p for p in self.config['people']}
        for login in ['ctsa','pkrusche','egor-dolzhenko']:
            self.assertEqual(people[login]['verification']['affiliation']['current'], 'unconfirmed')
        self.assertIn('alumnus', people['egor-dolzhenko']['verification']['affiliation']['past_contribution'])
        for p in people.values():
            aff = p['verification']['affiliation']
            if aff['status'] == 'verified_primary_source':
                self.assertTrue(aff['sources'])
                self.assertTrue(any('github.com/' not in u for u in aff['sources']))
            else: self.assertEqual(aff['current'],'unconfirmed')
        self.assertIn('Arc Institute',people['alexdobin']['verification']['affiliation']['current'])
        self.assertIn('Chief Architect',people['pditommaso']['verification']['affiliation']['current'])

if __name__ == '__main__': unittest.main()
