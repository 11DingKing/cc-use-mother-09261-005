import unittest
from service_09261_005.workflow import Workflow
class TestFlow(unittest.TestCase):
 def test_flow(self):
  f=Workflow(); self.assertEqual(f.create("c1","a","k").state,"draft"); self.assertEqual(f.create("c1","a","k").version,1); self.assertEqual(f.move("c1","reviewing","b").state,"reviewing")
if __name__=="__main__": unittest.main()
