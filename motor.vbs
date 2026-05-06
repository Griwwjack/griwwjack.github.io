Option Explicit

Dim http, fso, arquivo, urlLogin, urlDados, html, vs, vsg, ev, postData

urlLogin = "http://192.168.4.231:81/default.aspx"
urlDados = "http://192.168.4.231:81/request.aspx?name=SorterOrders"

Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
Set fso = CreateObject("Scripting.FileSystemObject")

' Configurações de tempo de espera (timeout)
http.SetTimeouts 5000, 5000, 10000, 10000

Do
    On Error Resume Next
    
    ' 1. PEGAR OS TOKENS INICIAIS
    http.Open "GET", urlLogin, False
    http.Send
    html = http.ResponseText

    ' Extração precisa dos valores ocultos 
    vs = URLEncode(Split(Split(html, "__VIEWSTATE"" value=""")(1), """")(0))
    vsg = URLEncode(Split(Split(html, "__VIEWSTATEGENERATOR"" value=""")(1), """")(0))
    ev = URLEncode(Split(Split(html, "__EVENTVALIDATION"" value=""")(1), """")(0))

    ' 2. MONTAR O POST COM ENCODING CORRETO 
    postData = "__VIEWSTATE=" & vs & "&__VIEWSTATEGENERATOR=" & vsg & "&__EVENTVALIDATION=" & ev & _
               "&ABnet_Form=ABnet_Login&ABnet_field_username=abnet&ABnet_field_password=abnet&ABnet_btn_Login=Entrar"

    ' 3. EXECUTAR LOGIN
    http.Open "POST", urlLogin, False
    http.SetRequestHeader "Content-Type", "application/x-www-form-urlencoded"
    http.Send postData

    ' 4. LOOP DE CAPTURA (Tenta 20 vezes antes de renovar o login)
    Dim i
    For i = 1 To 20
        http.Open "GET", urlDados, False
        http.Send
        
        ' Só grava se o retorno não for a página de login de novo
        If InStr(http.ResponseText, "ABnet_Login") = 0 Then
            Set arquivo = fso.CreateTextFile("dados_abnet.txt", True)
            arquivo.Write http.ResponseText
            arquivo.Close
        End If
        
        WScript.Sleep 5000 
    Next
    
    If Err.Number <> 0 Then Err.Clear
Loop

' Função essencial para converter caracteres do ASP.NET [cite: 2]
Function URLEncode(str)
    Dim i, c, charCode
    Dim res: res = ""
    For i = 1 To Len(str)
        c = Mid(str, i, 1)
        charCode = Asc(c)
        If (charCode >= 48 And charCode <= 57) Or _
           (charCode >= 65 And charCode <= 90) Or _
           (charCode >= 97 And charCode <= 122) Then
            res = res & c
        Else
            res = res & "%" & Hex(charCode)
        End If
    Next
    URLEncode = res
End Function