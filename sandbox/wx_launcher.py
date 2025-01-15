import wx
import wx.propgrid as wxpg
import os
import subprocess
 
class MyTree(wx.TreeCtrl):
    def __init__(self, *args, **kwargs):
        super(MyTree, self).__init__(*args, **kwargs)
        self.__collapsing = True
 
        il = wx.ImageList(16,16)
        self.folderidx = il.Add(wx.ArtProvider.GetBitmap(wx.ART_FOLDER, wx.ART_OTHER, (16,16)))
        self.fileidx = il.Add(wx.ArtProvider.GetBitmap(wx.ART_NORMAL_FILE, wx.ART_OTHER, (16,16)))
        self.AssignImageList(il)
 
        root = os.path.dirname(os.path.abspath(__file__))
        ids = {root : self.AddRoot(root, self.folderidx)}
        self.SetItemHasChildren(ids[root])
 
        for (dirpath, dirnames, filenames) in os.walk(root):
            for dirname in sorted(dirnames):
                fullpath = os.path.join(dirpath, dirname)
                ids[fullpath] = self.AppendItem(ids[dirpath], dirname, self.folderidx)
                 
            for filename in sorted(filenames):
                if filename[-3:] == ".py":
                  self.AppendItem(ids[dirpath], filename, self.fileidx)

class MainFrame(wx.Frame):
    def __init__(self):
        wx.Frame.__init__(self, None, title='Script Runner', size=(800,400))
        #panel = wx.Panel(self)
    
        self.files = MyTree(self)
        self.files.ExpandAll()
        self.files.Bind(wx.EVT_TREE_SEL_CHANGED, self.OnSelChanged)
        self.files.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self.run_file)

        bsizer1 = wx.BoxSizer(wx.VERTICAL)

        box1 = wx.StaticBoxSizer(wx.VERTICAL, self, "Description")
        self.txt = wx.StaticText(box1.GetStaticBox(), wx.ID_ANY,
                         "Please choose a script.")
        self.txt.Wrap(400)
        box1.Add(self.txt)

        self.pg = wxpg.PropertyGrid(self, style=wxpg.PG_SPLITTER_AUTO_CENTER)# |
                          #    wxpg.PG_AUTO_SORT)# |
                             # wxpg.PG_TOOLBAR)

        self.pg.Bind( wxpg.EVT_PG_CHANGED, self.OnPropGridChange )
        #pg.Bind( wx.propgrid.EVT_PG_PAGE_CHANGED, self.OnPropGridPageChange )
        self.pg.Bind( wxpg.EVT_PG_SELECTED, self.OnPropGridSelect )
        #pg.Bind( wx.propgrid.EVT_PG_RIGHT_CLICK, self.OnPropGridRightClick )
        lbl = wx.StaticText(self,-1,style = wx.ALIGN_LEFT) 
        txt = "Script Parameters"
        #font = wx.Font(18, wx.ROMAN, wx.ITALIC, wx.NORMAL) 
        #lbl.SetFont(font) 
        lbl.SetLabel(txt) 
        #self.pg.Append( wxpg.PropertyCategory("Script Parameters") )
        self.pg.Append( wxpg.IntProperty("Int", value=100) )
        self.pg.Append( wxpg.FloatProperty("Float", value=123.456) )
        """
        pg.Append( wxpg.BoolProperty("Bool_with_Checkbox", value=True) )
        pg.SetPropertyAttribute(
            "Bool_with_Checkbox",    # You can find the property by name,
            #boolprop,               # or give the property object itself.
            "UseCheckbox", True)     # The attribute name and value
        """
        bsizer1.Add(box1, 0, wx.EXPAND | wx.ALL, 10)
        bsizer1.Add(lbl, 0, wx.EXPAND | wx.ALL, 10)
        bsizer1.Add(self.pg, 0, wx.EXPAND | wx.ALL, 10)

        status_btn = wx.Button(self, label='Run')
        status_btn.Bind(wx.EVT_BUTTON, self.run_file)
 
        restore_btn = wx.Button(self, label='Edit')
        restore_btn.Bind(wx.EVT_BUTTON, self.edit_file)
 
        bsizer2 = wx.BoxSizer(wx.VERTICAL)
        bsizer2.AddSpacer(10)
        bsizer2.Add(status_btn, 0, wx.ALL, 5)
        bsizer2.Add(restore_btn, 0, wx.ALL, 5)

        sz = wx.FlexGridSizer(cols=3, hgap=5, vgap=5)
        sz.Add(self.files, 0, wx.EXPAND)
        sz.Add(bsizer1, 0, wx.EXPAND)
        sz.Add(bsizer2, 0, wx.EXPAND)

        sz.AddGrowableRow(0)
        sz.AddGrowableCol(0)
        sz.AddGrowableCol(1)
        sz.AddGrowableCol(2)

        self.SetSizer(sz)
        self.SetAutoLayout(True)

        self.statusbar = self.CreateStatusBar(2)
        self.statusbar.SetStatusWidths([100, -1])
        self.statusbar.SetStatusText('Status:')
        self.statusbar.SetStatusText('Ready', 1)
 
        self.Show()
    
    def run_file(self, event):
        if self.item:
            # Test for file (not dir)
            # Exec the python script using the wxpg parameters
            self.statusbar.SetStatusText("Running: %s\n" % self.files.GetItemText(self.item),1)
            l = ['python3', self.files.GetItemText(self.item)]
            d = self.pg.GetPropertyValues(as_strings=True)
            #print(d)
            for k,v in d.items():
              l.append(str(v))
            #print(l)
            subprocess.call(l)
 
    def edit_file(self, event):
        if self.item:
            # Open selected file with default editor
            self.statusbar.SetStatusText("Editing: %s\n" % self.files.GetItemText(self.item),1)
            l = ['code', self.files.GetItemText(self.item)]
            subprocess.call(l)
 
    def OnSelChanged(self, event):
        self.item = event.GetItem()
        if self.item:
            # Test for file (not dir)
            # Delete all wxpg entries
            # Parse file for DESCRIPTION(@brief) -> wx.StaticText (txt)
            # Parse file for PARAMETERS(@param) -> wxpg
            
            self.txt.SetLabel('')
            self.statusbar.SetStatusText("OnSelChanged: %s\n" % self.files.GetItemText(self.item),1)
            with open(self.files.GetItemText(self.item), 'r') as searchfile:
              for line in searchfile:
                  if '@brief' in line:
                      #print(line.find('@brief'))
                      self.txt.SetLabel(line[line.find('@brief')+7:])
                      self.txt.Wrap(400)

            self.pg.Clear()
            with open(self.files.GetItemText(self.item), 'r') as searchfile:
              for line in searchfile:
                  if '@param' in line:
                      # @param({int|float|string},default) Parameter description here
                      #print(line)
                      if 'int' in line[line.find('('):line.find(',')]:
                        self.pg.Append( wxpg.IntProperty(line[line.find(')')+2:-1], value=int(line[line.find(',')+1:line.find(')')])))
                      if 'float' in line[line.find('('):line.find(',')]:
                        self.pg.Append( wxpg.FloatProperty(line[line.find(')')+2:-1], value=float(line[line.find(',')+1:line.find(')')])))
                      if 'string' in line[line.find('('):line.find(',')]:  
                        self.pg.Append( wxpg.StringProperty(line[line.find(')')+2:-1], value=line[line.find(',')+1:line.find(')')]))
                      #self.txt.SetLabel(line)

    def OnPropGridChange(self, event):
        p = event.GetProperty()
        if p:
            self.statusbar.SetStatusText('%s changed to "%s"\n' % (p.GetName(),p.GetValueAsString()),1)

    def OnPropGridSelect(self, event):
        p = event.GetProperty()
        if p:
            self.statusbar.SetStatusText('%s selected\n' % (event.GetProperty().GetName()),1)
        else:
            self.statusbar.SetStatusText('Nothing selected\n',1)

if __name__ == '__main__':
    app = wx.App(False)
    frame = MainFrame()
    app.MainLoop()