# 모던 폴더 선택 대화상자 (탐색기 스타일, IFileOpenDialog + FOS_PICKFOLDERS).
# 실패 시 구형 FolderBrowserDialog 폴백. 선택 경로를 stdout으로 출력, 취소 = 출력 없음.
param([string]$Initial = "")

[Console]::OutputEncoding = [Text.Encoding]::UTF8

$code = @"
using System;
using System.Runtime.InteropServices;

public static class ModernFolderPicker {
    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    private class FileOpenDialogRCW { }

    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog {
        [PreserveSig] uint Show(IntPtr hwndParent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint fos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, uint fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath, IntPtr pbc,
        ref Guid riid, out IShellItem ppv);

    private const uint FOS_PICKFOLDERS = 0x20;
    private const uint FOS_FORCEFILESYSTEM = 0x40;
    private const uint SIGDN_FILESYSPATH = 0x80058000;

    public static string Pick(string title, string initial) {
        var dlg = (IFileDialog)new FileOpenDialogRCW();
        dlg.SetOptions(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM);
        dlg.SetTitle(title);
        if (!string.IsNullOrEmpty(initial)) {
            try {
                var iid = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
                IShellItem item;
                SHCreateItemFromParsingName(initial, IntPtr.Zero, ref iid, out item);
                dlg.SetFolder(item);
            } catch { }
        }
        if (dlg.Show(IntPtr.Zero) != 0) return null; // 취소
        IShellItem result;
        dlg.GetResult(out result);
        string path;
        result.GetDisplayName(SIGDN_FILESYSPATH, out path);
        return path;
    }
}
"@

try {
    Add-Type -TypeDefinition $code -ErrorAction Stop
    $p = [ModernFolderPicker]::Pick("폴더 선택", $Initial)
    if ($p) { Write-Output $p }
} catch {
    # 폴백: 구형 트리 다이얼로그
    Add-Type -AssemblyName System.Windows.Forms
    $f = New-Object System.Windows.Forms.FolderBrowserDialog
    $f.Description = '폴더 선택'
    if ($Initial) { $f.SelectedPath = $Initial }
    $top = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; WindowState = 'Minimized'; ShowInTaskbar = $false}
    if ($f.ShowDialog($top) -eq 'OK') { Write-Output $f.SelectedPath }
}
