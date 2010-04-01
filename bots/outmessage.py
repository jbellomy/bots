import time
try:
    import cElementTree as ET
except ImportError:
    try:
        import elementtree.ElementTree as ET
    except ImportError:
        try:
            from xml.etree import cElementTree as ET
        except ImportError:
            from xml.etree import ElementTree as ET
try:
    import elementtree.ElementInclude as ETI
except ImportError:
    from xml.etree import ElementInclude as ETI
#bots-modules
import botslib
import botsglobal
import grammar
import message
import node
from botsconfig import *

def outmessage_init(**ta_info):
    ''' despatch function class Outmessage or subclass
        ta_info: needed is editype, messagetype, filename, charset, merge
    '''
    try:
        classtocall = globals()[ta_info['editype']]
    except KeyError:
        raise botslib.OutMessageEdiTypeNotKnownError(ta_info['editype'])
    return classtocall(ta_info)

class Outmessage(message.Message):
    ''' abstract class; represents a outgoing edi message.
        subclassing is neccessary for the editype (csv, edi, x12, etc)
        A tree of nodes is build form the mpaths received from put()or putloop(). tree starts at self.root.
        Put() recieves mpaths from mappingscript
        The next algorithm is used to 'map' a mpath into the tree:
            For each part of a mpath: search node in 'current' level of tree
                If part already as a node: 
                    recursively search node-children
                If part not as a node:
                    append new node to tree;
                    recursively append next parts to tree
        After the mapping-script is finished, the resulting tree is converted to records (self.records).
        These records are written to file.
        Structure of self.records:
            list of record;
            record is list of field
            field is dict. Keys in field:
            -   ID       field ID (id within this record). For in-file
            -   VALUE    value, content of field
            -   MPATH    mpath of record, only for first field(=recordID)
            -   LIN     linenr of field in in-file
            -   POS      positionnr within line in in-file
            -   SFIELD   True if subfield (edifact-only)
            first field for record is recordID.
    '''
    def __init__(self,ta_info):
        self.ta_info = ta_info
        self.root = node.Node({})         #message tree; build via put()-interface in mapping-script. Initialise with empty dict
        super(Outmessage,self).__init__()

    def outmessagegrammarread(self,editype,messagetype):
        ''' read the grammar for a out-message.
            try to read the topartner dependent grammar syntax.
        ''' 
        self.defmessage = grammar.grammarread(editype,messagetype)
        #~ print 'self.ta_info',self.ta_info
        #~ print 'self.defmessage.syntax',self.defmessage.syntax
        botslib.updateunlessset(self.ta_info,self.defmessage.syntax)    #write values from grammar to self.ta_info - unless these values are already set eg by mapping script
        if self.ta_info['topartner']:   #read syntax-file for partner dependent syntax
            try:
                botsglobal.logger.debug(u'(try) to read partner specific syntax editype "%s", messagetype "%s".',editype,self.ta_info['topartner'])
                partnersyntax = grammar.syntaxread('partners',editype,self.ta_info['topartner'])
                self.ta_info.update(partnersyntax.syntax) #partner syntax overrules!
            except ImportError:
                botsglobal.logger.debug(u'No partner specific syntax found (is not an error).')

    def writeall(self):
        ''' writeall is called for writing all 'real' outmessage objects; but not for envelopes.
            writeall is call from transform.translate()
        '''
        self.outmessagegrammarread(self.ta_info['editype'],self.ta_info['messagetype'])
        self.nrmessagewritten = 0
        if self.root.record:        #root record contains information; write whole tree in one time
            self.multiplewrite = False
            self.normalisetree(self.root)
            self._initwrite()
            self._write(self.root)
            self.nrmessagewritten = 1
            self._closewrite()
        elif not self.root.children:
            raise botslib.OutMessageError(u'No outgoing message')    #then there is nothing to write...
        else:
            self.multiplewrite = True
            for childnode in self.root.children:
                self.normalisetree(childnode)
            self._initwrite()
            for childnode in self.root.children:
                self._write(childnode)
                self.nrmessagewritten += 1
            self._closewrite()

    def _initwrite(self):
        botsglobal.logger.debug(u'Start writing to file "%s".',self.ta_info['filename'])
        self._outstream = botslib.opendata(self.ta_info['filename'],'wb',charset=self.ta_info['charset'],errors=self.ta_info['checkcharsetout'])
        
    def _closewrite(self):
        botsglobal.logger.debug(u'End writing to file "%s".',self.ta_info['filename'])
        self._outstream.close()

    def _write(self,node):
        ''' the write method for most classes.
            tree is serialised to sequental records; records are written to file.
            Classses that write using other libraries (xml, json, template, db) use specific write methods. 
        ''' 
        self.tree2records(node)
        self._records2file()

    def tree2records(self,node):
        self.records = []                   #tree of nodes is flattened to these records
        self._tree2recordscore(node,self.defmessage.structure[0])
        
    def _tree2recordscore(self,node,structure):
        ''' Write tree of nodes to flat records.
            The nodes are already sorted 
        '''
        self._tree2recordfields(node.record,structure)    #write root node->first record
        for childnode in node.children:            #for every node in mpathtree, these are already sorted#SPEED: node.children is already sorted!
            for structure_record in structure[LEVEL]:  #for structure_record of this level in grammar
                if childnode.record['BOTSID'].strip() == structure_record[ID]:   #if is is the right node:
                    self._tree2recordscore(childnode,structure_record)         #use rest of index in deeper level

    def _tree2recordfields(self,noderecord,structure_record):
        ''' appends fields in noderecord to (raw)record; use structure_record as guide.
            complex because is is used for: editypes that have compression rules (edifact), var editypes without compression, fixed protocols 
        '''
        buildrecord = []
        buffer = []
        for grammarfield in structure_record[FIELDS]:
            if grammarfield[ISFIELD]:    #if field (no composite)
                if grammarfield[ID] in noderecord:
                    buildrecord.extend(buffer)
                    buffer=[]
                    buildrecord += [{VALUE:noderecord[grammarfield[ID]],SFIELD:False}]      #append new field
                else:
                    if self.ta_info['stripfield_sep']:
                        buffer += [{VALUE:'',SFIELD:False}]      #generate & append new field to buffer
                    else:
                        value = self._formatfield('',grammarfield,structure_record)  #generate field
                        buildrecord += [{VALUE:value,SFIELD:False}]                #append new field
            else:               #if composite
                donefirst = False
                subbuffer=[]
                subiswritten=False
                for grammarsubfield in grammarfield[SUBFIELDS]:   #loop subfields
                    if grammarsubfield[ID] in noderecord:
                        buildrecord.extend(buffer)
                        buffer=[]
                        buildrecord.extend(subbuffer)
                        subbuffer=[]
                        buildrecord += [{VALUE:noderecord[grammarsubfield[ID]],SFIELD:donefirst}]      #append field
                        subiswritten = True    
                    else:
                        subbuffer += [{VALUE:self._formatfield('',grammarsubfield,structure_record),SFIELD:donefirst}]      #generate & append new field
                    donefirst = True
                if not subiswritten:
                    buffer += [{VALUE:'',SFIELD:False}]
        self.records += [buildrecord]
                

    def _formatfield(self,value, grammarfield,record):
        ''' Input: value (as a string) and field definition.
            Some parameters of self.syntax are used: decimaal
            Format is checked and converted (if needed).
            return the formatted value
        '''
        #~ value = value[:]
        if grammarfield[BFORMAT] == 'A':
            value = value.ljust(grammarfield[MINLENGTH])    #add spaces (left, because A-field is right aligned)
            valuelength=len(value)
            if valuelength > grammarfield[LENGTH]:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" too big (max $max): "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH],max=grammarfield[LENGTH])
        elif grammarfield[BFORMAT] == 'D':
            try:
                lenght = len(value)
                if lenght==6:
                    time.strptime(value,'%y%m%d')
                elif lenght==8:
                    time.strptime(value,'%Y%m%d')
                else:
                    raise ValueError
            except ValueError:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" no valid date: "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH])
            valuelength=len(value)
            if valuelength > grammarfield[LENGTH]:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" too big (max $max): "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH],max=grammarfield[LENGTH])
            if valuelength < grammarfield[MINLENGTH]:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" too small (min $min): "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH],min=grammarfield[MINLENGTH])
        elif grammarfield[BFORMAT] == 'T':
            try:
                lenght = len(value)
                if lenght==4:
                    time.strptime(value,'%H%M')
                elif lenght==6:
                    time.strptime(value,'%H%M%S')
                else: #lenght==8:     #tsja...just use first part of field
                    raise ValueError
            except  ValueError:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" no valid time: "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH])
            valuelength=len(value)
            if valuelength > grammarfield[LENGTH]:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" too big (max $max): "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH],max=grammarfield[LENGTH])
            if valuelength < grammarfield[MINLENGTH]:
                raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" too small (min $min): "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH],min=grammarfield[MINLENGTH])
        else:   #numerics
            #calculatelengthwithdecimalsign and calculatelengthwithminussign: used to calcute the right field length (instead of True,False use 1,0 for field lengths calculations
            #self.ta_info['lengthnumericbare']: True: interpretate field elngths without (decimals sign, minus sign) 
            if value or isinstance(self,fixed):    #empty string for non-fixed is returned. Later on, ta_info[stripemptyfield] determines what to do with them
                if not value:   #see last if; if a numerical fixed field has content '' , change this to '0' (init)
                    value='0'
                try:
                    fvalue = float(value)
                except:
                    raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field" numerical format not valid: "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH])
                if value[0]=='-':   
                    if self.ta_info['lengthnumericbare']:
                        calculatelengthwithminussign=1
                    else:
                        calculatelengthwithminussign=0
                    if fvalue==0:   #make sure -0 stays negative. in python2.4 and 2.5 '-0' gets 0. in python 2.6 '-0' gets -0.
                        fvalue = abs(fvalue)    #make sure -0 is 0 (correction for python 2.6)
                        if not self.ta_info['lengthnumericbare']:
                            calculatelengthwithminussign-=1         #dirty. as we will add the minus sign later
                        iszeronegative = True
                    else:
                        iszeronegative = False
                else:
                    calculatelengthwithminussign = 0
                    iszeronegative = False
                if grammarfield[BFORMAT] == 'R':    #use all needed decimals
                    lendecimal = len(value[value.find('.'):])-1
                    if lendecimal and self.ta_info['lengthnumericbare']:
                        calculatelengthwithdecimalsign=1
                    else:
                        calculatelengthwithdecimalsign=0
                    value='%0*.*F'%(grammarfield[MINLENGTH]+calculatelengthwithminussign+calculatelengthwithdecimalsign,lendecimal,fvalue)
                    value = value.replace('.',self.ta_info['decimaal'],1)    #replace '.' by required decimal sep.
                elif grammarfield[BFORMAT] == 'N':  #fixed decimals
                    if grammarfield[DECIMALS] and self.ta_info['lengthnumericbare']:
                        calculatelengthwithdecimalsign=1
                    else:
                        calculatelengthwithdecimalsign=0
                    value='%0*.*F'%(grammarfield[MINLENGTH]+calculatelengthwithminussign+calculatelengthwithdecimalsign,grammarfield[DECIMALS],fvalue)
                    value = value.replace('.',self.ta_info['decimaal'],1)    #replace '.' by required decimal sep.
                elif grammarfield[BFORMAT] == 'I':  #implicit decimals
                    calculatelengthwithdecimalsign=0
                    fvalue = fvalue * 10**grammarfield[DECIMALS]
                    value='%0*.0F'%(grammarfield[MINLENGTH]+calculatelengthwithminussign,fvalue)
                    value = value.replace('.',u'',1)
                if iszeronegative:
                    value = '-' + value
                    calculatelengthwithminussign+=1
                #~ print value, len(value), calculatelengthwithdecimalsign,calculatelengthwithdecimalsign, grammarfield[LENGTH]
                if len(value)-calculatelengthwithminussign-calculatelengthwithdecimalsign > grammarfield[LENGTH]:
                    raise botslib.OutMessageFieldFormatError(u'record "$mpath" field "$field": content to large: "$content".',field=grammarfield[ID],content=value,mpath=record[MPATH])
        return value


    def _records2file(self):
        ''' convert self.records to a file.
            using the right editype (edifact, x12, etc) and charset.
        '''
        for record in self.records:     #loop all records
            try:
                self._outstream.write(self._record2string(record))
            except UnicodeEncodeError, flup:
                raise botslib.OutMessageCharsetError(u'Chars in outmessage not in charset "$char": $content',char=self.ta_info['charset'],content=flup)
                #code before 7 aug 2007 had other handling for flup. May have changed because python2.4->2.5?

    def _record2string(self,record):
        ''' write (all fields of) a record using the right separators, escape etc
        '''
        sfield_sep = self.ta_info['sfield_sep']
        if self.ta_info['record_tag_sep']:
            record_tag_sep = self.ta_info['record_tag_sep']
        else:
            record_tag_sep = self.ta_info['field_sep']
        field_sep = self.ta_info['field_sep']
        quote_char = self.ta_info['quote_char']
        escape = self.ta_info['escape']
        record_sep = self.ta_info['record_sep'] + self.ta_info['add_crlfafterrecord_sep']
        forcequote = self.ta_info['forcequote']
        escapechars = self.getescapechars()
        value = u''     #to collect separator/escape plus field content 
        fieldcount = 0
        mode_quote = False
        if self.ta_info['noBOTSID']:  #for some csv-files: do not write BOTSID so remove it
            del record[0]
        for field in record:        #loop all fields in record
            if field[SFIELD]:
                value += sfield_sep
            else:   #is a field: 
                if fieldcount == 0:  #do nothing because first field in record is not preceeded by a separator
                    fieldcount = 1
                elif fieldcount == 1:
                    value += record_tag_sep
                    fieldcount = 2
                else:
                    value += field_sep
            if  quote_char and (forcequote or field_sep in field[VALUE] or quote_char in field[VALUE] or record_sep in field[VALUE]):
                #TO DO test. if quote_char='' this works OK. Alt: check first if quote_char
                value += quote_char
                mode_quote = True
            for char in field[VALUE]:   #use escape (edifact, tradacom). For x12 is warned if content contains separator
                if char in escapechars:
                    if isinstance(self,x12):
                        if self.ta_info['replacechar']:
                            char = self.ta_info['replacechar']
                        else:
                            raise botslib.OutMessageCharsetError(u'Character "$char" is in use as separator in this x12 file. Field: "$data".',char=char,data=field[VALUE])
                    else:
                        value +=escape
                elif mode_quote and char==quote_char:
                    value +=quote_char
                value += char
            if mode_quote:
                value += quote_char
                mode_quote = False
        value += record_sep
        return value

    def getescapechars(self):
        return ''

class fixed(Outmessage):
    pass
        
        
class idoc(fixed):
    def _canonicalfields(self,noderecord,structure_record,headerrecordnumber):
        if self.ta_info['automaticcount']:
            noderecord.update({'MANDT':self.ta_info['MANDT'],'DOCNUM':self.ta_info['DOCNUM'],'SEGNUM':str(self.recordnumber),'PSGNUM':str(headerrecordnumber),'HLEVEL':str(len(structure_record[MPATH]))})
        else:
            noderecord.update({'MANDT':self.ta_info['MANDT'],'DOCNUM':self.ta_info['DOCNUM']})
        super(idoc,self)._canonicalfields(noderecord,structure_record,headerrecordnumber)
        self.recordnumber += 1      #tricky. EDI_DC is not counted, so I count after writing.


class var(Outmessage):
    pass


class csv(var):
    def getescapechars(self):
        return self.ta_info['escape']



class edifact(var):
    def getescapechars(self):
        terug = self.ta_info['record_sep']+self.ta_info['field_sep']+self.ta_info['sfield_sep']+self.ta_info['escape']
        if self.ta_info['version']>='4':
            terug += self.ta_info['reserve']
        return terug

class tradacoms(var):
    def getescapechars(self):
        terug = self.ta_info['record_sep']+self.ta_info['field_sep']+self.ta_info['sfield_sep']+self.ta_info['escape']+self.ta_info['record_tag_sep']
        return terug

    def writeall(self):
        ''' writeall is called for writing all 'real' outmessage objects; but not for enveloping.
            writeall is call from transform.translate()
        '''
        self.nrmessagewritten = 0
        if not self.root.children:
            raise botslib.OutMessageError(u'No outgoing message')    #then there is nothing to write...
        for message in self.root.getloop({'BOTSID':'STX'},{'BOTSID':'MHD'}):
            self.outmessagegrammarread(self.ta_info['editype'],message.get({'BOTSID':'MHD','TYPE.01':None}) + message.get({'BOTSID':'MHD','TYPE.02':None}))
            if not self.nrmessagewritten:
                self._initwrite()
            self.normalisetree(message)
            self._write(message)
            self.nrmessagewritten += 1
        self._closewrite()
        self.ta_info['nrmessages'] = self.nrmessagewritten


class x12(var):
    def getescapechars(self):
        terug = self.ta_info['record_sep']+self.ta_info['field_sep']+self.ta_info['sfield_sep']
        if self.ta_info['version']>='00403':
            terug += self.ta_info['reserve']
        return terug


class BotsElementTree(ET.ElementTree):
    ''' subclassing elementtree. reason: better xml prolog; doctype, processing instructions.
        also adds indenting.
        also needed when writing no internal edi file (in botssys/data)
    '''
    def write(self, file, encoding,ta_info=None):
        if not hasattr(file, "write"):
            file = botslib.opendata(file,"wb")
        if not encoding:
            encoding = 'us-ascii'
        #xml prolog
        if ta_info['standalone']:
            standalonestring = 'standalone="%s" '%(ta_info['standalone'])
        else:
            standalonestring = ''
        PI = ET.ProcessingInstruction('xml', 'version="%s" encoding="%s" %s'%(ta_info['version'],encoding, standalonestring))        
        file.write(ET.tostring(PI)) #do not use encoding here. gives double xml prolog; possibly because ET.ElementTree.write i used again by write()
        if ta_info['indented']:
            file.write('\n')
        #doctype
        if ta_info['DOCTYPE']:
            file.write('<!DOCTYPE %s>'%(ta_info['DOCTYPE']))
            if ta_info['indented']:
                file.write('\n')
        
        #processing instructions
        if ta_info['processing_instructions']:
            for pi in ta_info['processing_instructions']:
                PI = ET.ProcessingInstruction(pi[0], pi[1])
                file.write(ET.tostring(PI)) #do not use encoding here. gives double xml prolog; possibly because ET.ElementTree.write i used again by write()
                if ta_info['indented']:
                    file.write('\n')
                
        #indent the xml elements, starting with root    
        if ta_info['indented']:
            self.botsindent(self._root)
            
        self._write(file, self._root, encoding, {})
        file.close()

    def botsindent(self,elem, level=0,indentstring='    '):
        i = "\n" + level*indentstring
        if len(elem):
            if not elem.text or not elem.text.strip():
                elem.text = i + indentstring
            for e in elem:
                self.botsindent(e, level+1)
                if not e.tail or not e.tail.strip():
                    e.tail = i + indentstring
            if not e.tail or not e.tail.strip():
                e.tail = i
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = i
            

class xml(Outmessage):
    def _write(self,node):
        ''' write normal XML messages (no envelope)'''
        #~ xmltree = ET.ElementTree(self._node2xml(node))
        #~ node.display()
        xmltree = BotsElementTree(self._node2xml(node))
        xmltree.write(self.ta_info['filename'],encoding=self.ta_info['charset'],ta_info=self.ta_info)

    def envelopewrite(self,node):
        ''' write envelope for XML messages'''
        self.normalisetree(node)
        #~ xmltree = ET.ElementTree(self._node2xml(node))
        xmltree = BotsElementTree(self._node2xml(node))
        root=xmltree.getroot()
        ETI.include(root)
        xmltree.write(self.ta_info['filename'],encoding=self.ta_info['charset'],ta_info=self.ta_info)

    def _node2xml(self,node):
        ''' recursive method.
        '''
        newnode = self._node2xmlfields(node.record)
        for childnode in node.children:
            newnode.append(self._node2xml(childnode))
        return newnode

    def _node2xmlfields(self,noderecord):
        ''' fields in a node are written to xml fields; output is sorted according to grammar
        '''
        #first generate the xml-'record'
        attributedict = {}
        recordtag = noderecord['BOTSID']
        attributemarker = recordtag + self.ta_info['attributemarker'] 
        for key,value in noderecord.items():    #find the attributes for the xml-record, put these in attributedict
            if key.startswith(attributemarker):
                attributedict[key[len(attributemarker):]] = value
        xmlrecord = ET.Element(recordtag,attributedict) #make the xml ET node
        if 'BOTSCONTENT' in noderecord:
            xmlrecord.text = noderecord['BOTSCONTENT']
            del noderecord['BOTSCONTENT']
        for key in attributedict.keys():  #remove used fields
            del noderecord[attributemarker+key]
        del noderecord['BOTSID']    #remove 'record' tag
        #generate xml-'fields' in xml-'record'; sort these by looping over records definition
        for field_def in self.defmessage.recorddefs[recordtag]:  #loop over fields in 'record'
            if field_def[ID] not in noderecord: #if field not in outmessage: skip
                continue
            attributedict = {}
            attributemarker = field_def[ID] + self.ta_info['attributemarker'] 
            for key,value in noderecord.items():
                if key.startswith(attributemarker):
                    attributedict[key[len(attributemarker):]] = value
            ET.SubElement(xmlrecord, field_def[ID],attributedict).text=noderecord[field_def[ID]]    #add xml element to xml record
            for key in attributedict.keys():  #remove used fields
                del noderecord[attributemarker+key]
            del noderecord[field_def[ID]]    #remove xml entity tag
        return xmlrecord
        
    def _initwrite(self):
        botsglobal.logger.debug(u'Start writing to file "%s".',self.ta_info['filename'])
        
    def _closewrite(self):
        botsglobal.logger.debug(u'End writing to file "%s".',self.ta_info['filename'])



class xmlnocheck(xml):
    def normalisetree(self,node):
        pass

    def _node2xmlfields(self,noderecord):
        ''' fields in a node are written to xml fields; output is sorted according to grammar
        '''
        if 'BOTSID' not in noderecord:
            raise botslib.OutMessageError(u'No field "BOTSID" in xml-output in: "$record"',record=noderecord)
        #first generate the xml-'record'
        attributedict = {}
        recordtag = noderecord['BOTSID']
        attributemarker = recordtag + self.ta_info['attributemarker'] 
        for key,value in noderecord.items():    #find the attributes for the xml-record, put these in attributedict
            if key.startswith(attributemarker):
                attributedict[key[len(attributemarker):]] = value
        xmlrecord = ET.Element(recordtag,attributedict) #make the xml ET node
        if 'BOTSCONTENT' in noderecord:
            xmlrecord.text = noderecord['BOTSCONTENT']
            del noderecord['BOTSCONTENT']
        for key in attributedict.keys():  #remove used fields
            del noderecord[attributemarker+key]
        del noderecord['BOTSID']    #remove 'record' tag
        #generate xml-'fields' in xml-'record'; not sorted
        noderecordcopy = noderecord.copy()
        for key,value in noderecordcopy.items():
            if key not in noderecord or self.ta_info['attributemarker'] in key: #if field not in outmessage: skip
                continue
            attributedict = {}
            attributemarker = key + self.ta_info['attributemarker'] 
            for key2,value2 in noderecord.items():
                if key2.startswith(attributemarker):
                    attributedict[key2[len(attributemarker):]] = value2
            ET.SubElement(xmlrecord, key,attributedict).text=value    #add xml element to xml record
            for key2 in attributedict.keys():  #remove used fields
                del noderecord[attributemarker+key2]
            del noderecord[key]    #remove xml entity tag
        return xmlrecord
        

class json(Outmessage):
    def _initwrite(self):
        super(json,self)._initwrite()
        if self.multiplewrite:
            self._outstream.write(u'[')

    def _write(self,node):
        ''' convert node tree to appropriate python objecct.
            python objects are written to json by simplejson.
        '''
        import simplejson
        if self.nrmessagewritten:
            self._outstream.write(u',')
        jsonobject = {node.record['BOTSID']:self._node2json(node)}
        if self.ta_info['indented']:
            indent=2
        else:
            indent=None
        simplejson.dump(jsonobject, self._outstream, skipkeys=False, ensure_ascii=False, check_circular=False, indent=indent)

    def _closewrite(self):
        if self.multiplewrite:
            self._outstream.write(u']')
        super(json,self)._closewrite()

    def _node2json(self,node):
        ''' recursive method.
        '''
        #newjsonobject is the json object assembled in the function.
        newjsonobject = node.record.copy()    #init newjsonobject with record fields from node 
        for childnode in node.children: #fill newjsonobject with the records from childnodes. 
            key=childnode.record['BOTSID']
            if key in newjsonobject:
                newjsonobject[key].append(self._node2json(childnode))
            else:
                newjsonobject[key]=[self._node2json(childnode)]
        del newjsonobject['BOTSID']
        return newjsonobject
        
    def _node2jsonold(self,node):
        ''' recursive method.
        '''
        newdict = node.record.copy()
        if node.children:   #if this node has records in it.
            sortedchildren={}   #empty dict 
            for childnode in node.children:
                botsid=childnode.record['BOTSID']
                if botsid in sortedchildren:
                    sortedchildren[botsid].append(self._node2json(childnode))
                else:
                    sortedchildren[botsid]=[self._node2json(childnode)]
            for key,value in sortedchildren.items():
                if len(value)==1:
                    newdict[key]=value[0]
                else:
                    newdict[key]=value
        del newdict['BOTSID']
        return newdict
        
class jsonnocheck(json):
    def normalisetree(self,node):
        pass

class template(Outmessage):
    class TemplateData(object):
        pass
    def __init__(self,ta_info):
        self.data = template.TemplateData() #self.dat is used by mapping script as container for content
        super(template,self).__init__(ta_info)

    def writeall(self):
        ''' Very different writeall:
            there is no tree of nodes; there is no grammar.structure/recorddefs; kid opens file by itself.
        '''
        import kid
        #for template-grammar: only syntax is used. Section 'syntax' has to have 'template'
        self.outmessagegrammarread(self.ta_info['editype'],self.ta_info['messagetype'])
        templatefile = botslib.abspath(u'templates',self.ta_info['template'])
        try:
            botsglobal.logger.debug(u'Start writing to file "%s".',self.ta_info['filename'])
            ediprint = kid.Template(file=templatefile, data=self.data)
        except:
            txt=botslib.txtexc()
            raise botslib.OutMessageTemplateError(u'In "$editype.$messagetype": $txt',editype=self.ta_info['editype'],messagetype=self.ta_info['messagetype'],txt=txt)
        try:
            f = botslib.opendata(self.ta_info['filename'],'wb')
            ediprint.write(f,
            #~ ediprint.write(botslib.abspathdata(self.ta_info['filename']),
                            encoding=self.ta_info['charset'],
                            output=self.ta_info['output'],    #output is specific parameter for class; init from grammar.syntax
                            fragment=self.ta_info['merge'])
        except:
            txt=botslib.txtexc()
            raise botslib.OutMessageTemplateKidError(u'Kid error in "$editype.$messagetype": $txt',editype=self.ta_info['editype'],messagetype=self.ta_info['messagetype'],txt=txt)
        botsglobal.logger.debug(u'End writing to file "%s".',self.ta_info['filename'])


class database(jsonnocheck):
    pass
    