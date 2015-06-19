import gdb
import struct

def init():
    global types
    global dvm

    types = Types()
    dvm = Dvm()

    Debug.enabled = False

class Debug:
    enabled = False

    @staticmethod
    def p(s):
        if Debug.enabled:
            print "-- ", s

class Types:
    """Used as a symbol cache for performance"""
    def __init__(self):
        self._types = {}  # Caches for gdb pointer types

    def _pointer(self, type_name):
        return gdb.lookup_type(type_name).pointer()

    def __getitem__(self, key):
        """Returns pointer type specified by 'key'"""
        try:
            return self._types[key]
        except KeyError:
            self._types[key] = self._pointer(key)
        return self._types[key]

    def cast_p(self, addr, type_name):
        """Casts an address to specified type"""
        return gdb.Value(addr).cast(self[type_name])

class JavaBackTraceError(Exception):
    pass

class Dvm:
    """Holds global symbol gDvm and also various dvm*() functions"""
    STRING_FIELDOFF_COUNT = 20
    STRING_FIELDOFF_OFFSET = 16
    STRING_FIELDOFF_VALUE = 8

    LW_SHAPE_MASK = 0x1
    LW_SHAPE_THIN = 0

    LW_LOCK_OWNER_MASK = 0xffff
    LW_LOCK_OWNER_SHIFT = 3

    LW_HASH_STATE_MASK = 0x3
    LW_HASH_STATE_SHIFT = 1

    def __init__(self):
        self._dvm = gdb.parse_and_eval("gDvm")
        if not self._dvm:
            print "gDvm symbol not found"
            return

        self._field_map = {
            "Boolean":"z",
            "Byte":"b",
            "Short":"s",
            "Char":"c",
            "Int":"i",
            "Long":"j",
            "Float":"f",
            "Double":"d",
            "Object":"l"
        }

    def __getitem__(self, key):
        return self._dvm[key]

    def getFieldP(self, obj, offset, field):
        """See dvmGetField*() functions, need to convert to union member using the 'field'"""
        field_pointer = (obj.cast(types["char"]) + offset).cast(types["JValue"])
        return field_pointer.dereference()[self._field_map[field]]

    def _lockOwner(self, obj):
        """See lockOwner(), returns thread Id of lock owner"""
        lock = obj["lock"]
        tid = 0
        if (lock & Dvm.LW_SHAPE_MASK) == Dvm.LW_SHAPE_THIN:
            tid = (lock >> Dvm.LW_LOCK_OWNER_SHIFT) & Dvm.LW_LOCK_OWNER_MASK
        else:
            monitor_addr = (lock & ~((Dvm.LW_HASH_STATE_MASK << Dvm.LW_HASH_STATE_SHIFT) | \
                    Dvm.LW_SHAPE_MASK))
            owner = types.cast_p(monitor_addr, "struct Monitor")["owner"]
            if owner:
                tid = owner["threadId"]
        return tid

    def getThreadbyThreadId(self, threadId):
        """See dvmGetThreadByThreadId()"""
        thread_addr = self._dvm["threadList"]
        while thread_addr:
            thread = Thread(thread_addr)
            if thread["threadId"] == threadId:
                return thread
            thread_addr = thread_addr['next']

        return None

    def getObjectLockHolder(self, obj):
        """See dvmGetObjectLockHolder() function"""
        threadId = self._lockOwner(obj)
        if threadId:
            return self.getThreadbyThreadId(threadId)
        else:
            return None

    def createCstrFromString(self, s):
        """Refer to dvmCreateCstrFromString() in dalvik/vm/UtfString.cpp"""
        if not s:
            return ""

        length = self.getFieldP(s, Dvm.STRING_FIELDOFF_COUNT, "Int")
        offset = self.getFieldP(s, Dvm.STRING_FIELDOFF_OFFSET, "Int")
        chars = self.getFieldP(s, Dvm.STRING_FIELDOFF_VALUE, "Object").cast(types["ArrayObject"])
        data = chars["contents"].cast(types["u2"]) + offset
        assert (offset + length <= chars["length"])

        utf16_list = []
        for i in range(0, length):
            utf16_list.append(struct.pack('H', data.dereference()))
            data += 1

        return "".join(utf16_list).decode("utf-16")

    def humanReadableDescriptor(self, desc):
        """Simplified version of dvmHumanReadableDescriptor() of dalvik/vm/Misc.cpp"""
        out = desc[:-1] # First strip the ending ';'

        if out[0] == 'L':
            out = out[1:]

        return out.replace('/', '.')

    #FIXME: Finish other part of code in dvmHumanReadableType()
    def humanReadableType(self, obj):
        if not obj["clazz"]:
            return "(raw)"
        return self.humanReadableDescriptor(obj["clazz"]["descriptor"].string())

    def threadFromThreadObject(self, obj):
        return Thread(self.getFieldP(obj, self["offJavaLangVMThread_vmData"], "Int"))

class Thread:
    """Java Thread related members and methods"""
    statusList = ["ZOMBIE", "RUNNABLE", "TIMED_WAIT", "MONITOR", "WAIT",
                  "INITIALIZING", "STARTING", "NATIVE", "VMWAIT", "SUSPENDED"]

    def __init__(self, thread_addr):
        assert thread_addr

        self._thread = types.cast_p(thread_addr, "struct Thread")
        self._threadObj = self._thread["threadObj"]
        self._threadStatus = self._thread["status"]

    def __getitem__(self, key):
        return self._thread[key]

    def __str__(self):
        return str(self._thread)

    def obj(self):
        return self._threadObj

    def name(self):
        nameStr = dvm.getFieldP(self._threadObj, dvm["offJavaLangThread_name"], "Object")\
                  .cast(types["StringObject"])

        return dvm.createCstrFromString(nameStr)

    def status(self):
        status = self._threadStatus
        if status < 0 or status >= len(Thread.statusList):
            return "UNKNOWN"

        return Thread.statusList[int(status)]

    def curFrame(self):
        if self._thread["interpSave"]:
            return self._thread["interpSave"]["curFrame"]
        else:  #Old version of Android uses this structure?
            return self._thread["curFrame"]

class ThreadStackTrace:
    def __init__(self, thread_addr):
        self._thread = Thread(thread_addr)

    def _waitMessage(self, detail, obj, thread):
        s = "  - waiting %s <%s> " % (detail, obj)
        if obj and obj["clazz"] != dvm["classJavaLangClass"]:
            s += "(a " + dvm.humanReadableType(obj) + ")"

        if thread:
            s += " held by tid=%d (%s)" % (thread["threadId"], thread.name())

        return s + "\n"

    def _saveareaFromFP(self, frame_ptr):
        """See SAVEAREA_FROM_FP define"""
        return frame_ptr.cast(types["struct StackSaveArea"]) - 1

    def _extractMonitorEnterObject(self):
        """See extractMonitorEnterObject() function in Stack.cpp"""
        jfp = self._thread.curFrame()
        saveArea = self._saveareaFromFP(jfp)
        method = saveArea["method"]
        currentPc = saveArea["xtra"]["currentPc"].dereference()

        if (currentPc & 0xff) != 0x1d:  # OP_MONITOR_ENTER
            raise JavaBackTraceError("wrong currentPc value")

        reg = currentPc >> 8
        if reg > method["registersSize"]:
            raise JavaBackTraceError("invalid register %d (max %d)" %
                                  (reg, method["registersSize"]))

        obj = jfp.cast(types["u4"])[reg]
        if (not obj) or (obj & 7):
            raise JavaBackTraceError("invalid object %p at %p[%d]" % (obj, jfp, reg))

        obj = obj.cast(types["Object"])
        return (obj, dvm.getObjectLockHolder(obj))

    def __str__(self):
        """Main method to generate stack trace"""
        thread = self._thread
        jfp = thread.curFrame()

        isDeamon = dvm.getFieldP(thread.obj(), dvm["offJavaLangThread_daemon"],
                                       "Boolean")
        deamon_str = ""
        if isDeamon:
            deamon_str = " deamon"

        priority = dvm.getFieldP(thread.obj(), dvm["offJavaLangThread_priority"], "Int")
        s = "\"%s\"%s prio=%d tid=%d %s\n  | sysTid=%d self=%s\n" % \
             (thread.name(), deamon_str, priority, thread["threadId"],
              thread.status(), thread["systemTid"], thread)

        first = True
        while jfp:
            saveArea = self._saveareaFromFP(jfp)
            jmethodp = saveArea["method"]
            Debug.p("  method: %s" % jmethodp)
            if jmethodp:
                # One stack trace line
                s += "  at %s.%s" % \
                 (dvm.humanReadableType(jmethodp), jmethodp["name"].string())
                if jmethodp["accessFlags"] & 0x100:
                    s += " (Native method)"
                else:
                    # Access to "sourceFile" member can sometimes fail, catch this exception and
                    # ignore it
                    try:
                        s += " (%s)" % jmethodp["clazz"]["sourceFile"].string()
                    except gdb.MemoryError, e:
                        pass
                s += "\n"

                # Print 'wait' lock info for first line of stack trace
                if first:
                    if thread.status() == "WAIT" or thread.status() == "TIMED_WAIT":
                        mon = thread["waitMonitor"]
                        if mon:
                            obj = mon["obj"]
                            if obj:
                                joinThread = None
                                if obj["clazz"] == dvm["classJavaLangVMThread"]:
                                    joinThread = dvm.threadFromThreadObject(obj)
                                s += self._waitMessage("on", obj, joinThread)
                    elif thread.status() == "MONITOR":
                        obj, owner = self._extractMonitorEnterObject()
                        s += self._waitMessage("to lock", obj, owner)

                first = False

                jfp = saveArea["prevFrame"]
            else:
                jfp = None

        return s

class AllThreadsStackTrace:
    def __str__(self):
        s = ""
        thread_addr = dvm["threadList"]
        while thread_addr:
            Debug.p("thread %s:" % thread_addr)
            s += str(ThreadStackTrace(thread_addr))
            s += "\n"
            thread_addr = thread_addr['next']

        return s

class JavaBackTraceCommand (gdb.Command):
    """Show Android Dalvik stack trace from core dump file
If core dump file generated from crash command, must use 'gcore -f 31 <pid>' to generate core dump

Usage: jbt [thread]
   'thread' is a struct *Thread pointer address, or no parameter to dump all threads

Options:
   -d   Enable debug mode
   -h   Show this help message
"""
    def __init__ (self):
        super (JavaBackTraceCommand, self).__init__ ("jbt",
                                                      gdb.COMMAND_SUPPORT)

    def invoke (self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        init()
        if len(args) > 0:
            if args[0] == '-h' or args[0] == '--help':
                self.help()
                return
            elif args[0] == '-d':
                Debug.enabled = True
                args = args[1:]

            if len(args) > 1:
                self.help()
            elif len(args) == 1:
                print ThreadStackTrace(int(args[0], 16))
            else:
                print AllThreadsStackTrace()
        else:
            print AllThreadsStackTrace()

    def help(self):
        print self.__doc__

JavaBackTraceCommand ()
