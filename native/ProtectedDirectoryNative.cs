// Precompiled Dawnstrike support surface formerly embedded in scripts/protected_operation_contract.ps1.
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Security {
    public static class ProtectedDirectoryNative {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFileW(
            string path,
            UInt32 desiredAccess,
            UInt32 shareMode,
            IntPtr securityAttributes,
            UInt32 creationDisposition,
            UInt32 flagsAndAttributes,
            IntPtr templateFile
        );
    }
}
