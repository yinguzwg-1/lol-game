' 5v5 �����ǩ - һ����������Flask ���� + �����ǹ̶��������� + ��ʾȺ������ַ��
' ��Ϊ��������������ֱ�Ӵ��������δ������Ĭ���𣻾����󵯴���ʾȺ��������
' ���ģʽ��cscript //nologo ������ǩ.vbs test  ��ֻ�����ϲ����������������
Dim fso, shell, testMode
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = dir
testMode = (WScript.Arguments.Count > 0 And LCase(WScript.Arguments(0)) = "test")

Function alive(url)
  On Error Resume Next
  Dim http
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.open "GET", url, False
  http.send
  If Err.Number = 0 And http.Status = 200 Then alive = True Else alive = False
  Set http = Nothing
  On Error GoTo 0
End Function

' �� Flask �ӿ�ȡ��ǰ������ַ���ýӿ�ֻ��ָ�� 9529 �������������ô����������ַ��
Function publicUrl()
  On Error Resume Next
  Dim http, re, m
  publicUrl = ""
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.open "GET", "http://127.0.0.1:9529/api/public-url", False
  http.send
  If Err.Number = 0 Then
    Set re = New RegExp
    re.Pattern = """url"":""(https?://[^""]+)"""
    Set m = re.Execute(http.responseText)
    If m.Count > 0 Then publicUrl = m(0).SubMatches(0)
  End If
  Set http = Nothing
  On Error GoTo 0
End Function

Sub show(msg, kind)
  If testMode Then
    WScript.Echo msg
  ElseIf kind = "warn" Then
    MsgBox msg, vbExclamation, "5v5 �����ǩ"
  Else
    MsgBox msg, vbInformation, "5v5 �����ǩ"
  End If
End Sub

Dim i, url
url = ""

' 1) Flask ����9529����δ������Ĭ����bat ���� flask ȱʧ�Զ���װ��
If Not alive("http://127.0.0.1:9529/api/state") Then
  If testMode Then WScript.Echo "[���] Flask δ���У��������� service_start.bat ..."
  shell.Run """" & dir & "\service_start.bat""", 7, False
  For i = 1 To 40
    WScript.Sleep 1000
    If alive("http://127.0.0.1:9529/api/state") Then Exit For
  Next
End If
If testMode Then WScript.Echo "[���] Flask ���: " & alive("http://127.0.0.1:9529/api/state")

' 2) ������ַ�����Ȼ����ǹ̶����������ò��䣩����ͨ������ cpolar ��ʱ����������
url = publicUrl()
If url = "" Then
  If testMode Then WScript.Echo "[���] �̶�����δͨ���������� cpolar ��ʱ���������ã�..."
  shell.Run "cmd /c cd /d """ & dir & """ & cpolar\cpolar.exe http 9529", 7, False
  For i = 1 To 30
    WScript.Sleep 2000
    url = publicUrl()
    If url <> "" Then Exit For
  Next
End If
If testMode Then WScript.Echo "[���] ������ַ: " & url

' 3) �򿪱���ҳ�� + ������ʾȺ��������
If Not testMode Then shell.Run "http://127.0.0.1:9529/", 1, False
If url <> "" Then
  show "Ⱥ�������ӣ��Ѹ��ƣ���ֱ��ճ����΢��Ⱥ����" & vbCrLf & vbCrLf & url & vbCrLf & vbCrLf & "˵�������ǻ����ǹ̶����������ò��䣬��ʱ���á�", "info"
  On Error Resume Next
  Dim clipHtml
  Set clipHtml = CreateObject("htmlfile")
  clipHtml.parentWindow.clipboardData.setData "text", url
  On Error GoTo 0
Else
  show "Flask ������������������ͨ��δ������" & vbCrLf & vbCrLf & "���黨���ǿͻ����Ƿ������в��ѵ�¼���˺ţ������������� http://127.0.0.1:9529 ��", "warn"
End If