import unittest
from operator import itemgetter

from openquake.commonlib import readinput, writers
from openquake.risklib import riskinput, utils
from openquake.commonlib.calculators import event_based
from openquake.qa_tests_data.event_based_risk import case_2


def make_event_loss_table(output, tags):
    """
    :returns: a list [((tag, asset_id, loss), ...] for nonzero losses
    """
    rows = []
    all_losses = (output.loss_matrix.transpose() *
                  utils.numpy_map(lambda a: a.value(output.loss_type),
                                  output.assets))  # a matrix R x N
    asset_ids = [a.id for a in output.assets]
    for tag, losses in zip(tags, all_losses):
        for asset_id, loss in zip(asset_ids, losses):
            if loss:
                rows.append((tag, asset_id, loss))
    return rows


class RiskInputTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oqparam = readinput.get_oqparam(
            'job_haz.ini,job_risk.ini', pkg=case_2)
        cls.sitecol, cls.assets_by_site = readinput.get_sitecol_assets(
            cls.oqparam, readinput.get_exposure(cls.oqparam))
        cls.riskmodel = readinput.get_risk_model(cls.oqparam)

    def test_get_all(self):
        self.assertEqual(
            list(self.riskmodel.get_imt_taxonomies()),
            [('PGA', ['RM']), ('SA(0.2)', ['RC']), ('SA(0.5)', ['W'])])
        self.assertEqual(len(self.sitecol), 4)
        hazard_by_site = [None] * 4

        ri_PGA = self.riskmodel.build_input(
            'PGA', hazard_by_site, self.assets_by_site)
        assets, hazards, epsilons = ri_PGA.get_all()
        self.assertEqual([a.id for a in assets], ['a0', 'a3', 'a4'])
        self.assertEqual(set(a.taxonomy for a in assets), set(['RM']))
        self.assertEqual(epsilons, [None, None, None])

        ri_SA_02 = self.riskmodel.build_input(
            'SA(0.2)', hazard_by_site, self.assets_by_site)
        assets, hazards, epsilons = ri_SA_02.get_all()
        self.assertEqual([a.id for a in assets], ['a1'])
        self.assertEqual(set(a.taxonomy for a in assets), set(['RC']))
        self.assertEqual(epsilons, [None])

        ri_SA_05 = self.riskmodel.build_input(
            'SA(0.5)', hazard_by_site, self.assets_by_site)
        assets, hazards, epsilons = ri_SA_05.get_all()
        self.assertEqual([a.id for a in assets], ['a2'])
        self.assertEqual(set(a.taxonomy for a in assets), set(['W']))
        self.assertEqual(epsilons, [None])

    def test_from_ruptures(self):
        oq = self.oqparam
        correl_model = readinput.get_correl_model(oq)
        rupcalc = event_based.EventBasedRuptureCalculator(oq)
        # this is case with a single TRT
        [(trt_id, ses_ruptures)] = rupcalc.run()['ruptures_by_trt'].items()

        gsims = rupcalc.rlzs_assoc.get_gsims_by_trt_id()[trt_id]

        ri = self.riskmodel.build_input_from_ruptures(
            self.sitecol, self.assets_by_site, ses_ruptures,
            gsims, oq.truncation_level, correl_model)

        riskinput.set_epsilons(
            ri, len(ses_ruptures), oq.master_seed,
            getattr(oq, 'asset_correlation', 0))

        assets, hazards, epsilons = ri.get_all()
        self.assertEqual([a.id for a in assets],
                         ['a0', 'a1', 'a2', 'a3', 'a4'])
        self.assertEqual(set(a.taxonomy for a in assets),
                         set(['RM', 'RC', 'W']))
        self.assertEqual(map(len, epsilons), [20] * 5)

        data = {loss_type: [] for loss_type in self.riskmodel.get_loss_types()}
        for out_by_rlz in self.riskmodel.gen_outputs([ri], rupcalc.rlzs_assoc):
            [out] = out_by_rlz.values()
            elt = make_event_loss_table(out, ri.tags)
            data[out.loss_type].extend(elt)
        for loss_type in data:
            # sort by tag, asset_id
            sdata = sorted(data[loss_type], key=itemgetter(0, 1))
            writers.save_csv('elt-%s.csv' % loss_type, sdata)