import unittest
import numpy as np
from tools.check_logits import compare

class LogitChecks(unittest.TestCase):
    def setUp(self):
        self.reference=np.zeros((2,22),dtype=np.float32)
        self.reference[0,3]=1;self.reference[1,7]=1
    def test_same_labels_do_not_hide_numeric_drift(self):
        actual=self.reference*2
        self.assertFalse(compare(actual,self.reference,.1,1.0)['numeric_match'])
    def test_swapped_class_fails_even_with_loose_numeric_bound(self):
        actual=self.reference.copy();actual[0,3]=0;actual[0,4]=1
        self.assertFalse(compare(actual,self.reference,2.0,1.0)['numeric_match'])
    def test_non_finite_and_wrong_shape(self):
        with self.assertRaises(ValueError):compare(self.reference[:1],self.reference,.1,1)
        actual=self.reference.copy();actual[0,0]=np.nan
        with self.assertRaises(ValueError):compare(actual,self.reference,.1,1)
    def test_small_perturbation_passes(self):
        self.assertTrue(compare(self.reference+.001,self.reference,.01,1)['numeric_match'])

if __name__=='__main__':unittest.main()
