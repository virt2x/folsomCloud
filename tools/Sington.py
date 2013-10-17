import threading  
      
class Sing(object):  
    def __init__(self):
        Sing.getInst()

    __inst = None
  
    __lock = threading.Lock()
 
    @staticmethod  
    def getInst():  
        Sing.__lock.acquire()  
        if not Sing.__inst:
            print "New it here"
            Sing.__inst = object.__new__(Sing)  
            object.__init__(Sing.__inst)  
        Sing.__lock.release()  
        return Sing.__inst

    def f(self):
        print "ok"

c = Sing()
c.f()
d = Sing.getInst()
d.f()
e = Sing()
