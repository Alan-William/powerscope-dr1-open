import unittest
import numpy as np
import torch
from network import Student,inputs

class Contract(unittest.TestCase):
    def test_layout_and_integer_statistics(self):
        x=np.stack([np.ones((3,500)),np.zeros((3,500)),-np.ones((3,500))]).astype('f4')
        y=inputs(x,'stats')
        np.testing.assert_array_equal(y[:,:3].reshape(3,3,1024)[:,:,:500],x)
        np.testing.assert_array_equal(y[:,:3].reshape(3,3,1024)[:,:,500:],0)
        np.testing.assert_array_equal(y[:,6:],0)
        np.testing.assert_array_equal(y[:,3:6,0,0],x[:,:,0])
    def test_rep_fusion(self):
        torch.manual_seed(7);torch.set_num_threads(2)
        for rep in [False,True]:
            m=Student(rep=rep).eval();x=torch.randn(3,9,32,32)
            with torch.no_grad():torch.testing.assert_close(m(x),m.deploy()(x),rtol=1e-5,atol=1e-6)
    def test_noise_has_no_label_or_split_input(self):
        x=np.zeros((4,3,500),'f4');a=inputs(x,'noise',seed=2);b=inputs(x,'noise',seed=2)
        np.testing.assert_array_equal(a,b);self.assertTrue(np.any(a[:,3:]!=inputs(x,'noise',seed=3)[:,3:]))
if __name__=='__main__':unittest.main()
