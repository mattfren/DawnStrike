// Precompiled Dawnstrike support surface formerly embedded in scripts/invoke_dawnstrike_stage.ps1.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Locking {
    [StructLayout(LayoutKind.Sequential)]
    public struct FileDispositionInfo {
        [MarshalAs(UnmanagedType.Bool)]
        public bool DeleteFile;
    }

    public static class DailyLockNative {
        private const UInt32 GenericRead = 0x80000000;
        private const UInt32 GenericWrite = 0x40000000;
        private const UInt32 DeleteAccess = 0x00010000;
        private const UInt32 FileShareRead = 0x00000001;
        private const UInt32 CreateNew = 1;
        private const UInt32 OpenExisting = 3;
        private const UInt32 FileAttributeNormal = 0x00000080;
        private const UInt32 FileFlagOpenReparsePoint = 0x00200000;
        private const int FileRenameInformation = 3;
        private const int FileDispositionInformation = 4;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(
            string path,
            UInt32 desiredAccess,
            UInt32 shareMode,
            IntPtr securityAttributes,
            UInt32 creationDisposition,
            UInt32 flagsAndAttributes,
            IntPtr templateFile
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(
            SafeFileHandle file,
            int fileInformationClass,
            ref FileDispositionInfo fileInformation,
            UInt32 bufferSize
        );

        [DllImport(
            "kernel32.dll",
            EntryPoint = "SetFileInformationByHandle",
            SetLastError = true
        )]
        private static extern bool SetFileInformationByHandleRaw(
            SafeFileHandle file,
            int fileInformationClass,
            IntPtr fileInformation,
            UInt32 bufferSize
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern UInt32 GetFinalPathNameByHandleW(
            SafeFileHandle file,
            StringBuilder path,
            UInt32 pathLength,
            UInt32 flags
        );

        public static SafeFileHandle CreateNewRetained(string path) {
            // DELETE access lets explicit Exit mark this exact open file for
            // deletion. There is deliberately no delete-on-close flag: a hard
            // process crash must leave durable dead-owner recovery evidence.
            SafeFileHandle handle = CreateFileW(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                IntPtr.Zero,
                CreateNew,
                FileAttributeNormal,
                IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, "Retained daily lock creation failed");
            }
            return handle;
        }

        public static SafeFileHandle OpenExistingRetained(string path) {
            SafeFileHandle handle = CreateFileW(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                IntPtr.Zero,
                OpenExisting,
                FileAttributeNormal | FileFlagOpenReparsePoint,
                IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, "Retained daily lock open failed");
            }
            return handle;
        }

        public static void MarkDelete(SafeFileHandle handle) {
            FileDispositionInfo disposition = new FileDispositionInfo();
            disposition.DeleteFile = true;
            UInt32 size = (UInt32)Marshal.SizeOf(typeof(FileDispositionInfo));
            if (!SetFileInformationByHandle(
                handle,
                FileDispositionInformation,
                ref disposition,
                size
            )) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Retained daily lock exact deletion failed"
                );
            }
        }

        public static void RenameNoReplace(SafeFileHandle handle, string destination) {
            byte[] name = Encoding.Unicode.GetBytes(destination);
            int rootOffset = IntPtr.Size;
            int lengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = lengthOffset + 4;
            int size = nameOffset + name.Length + 2;
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                for (int index = 0; index < size; index++) Marshal.WriteByte(buffer, index, 0);
                Marshal.WriteByte(buffer, 0, 0); // ReplaceIfExists = false
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, lengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!SetFileInformationByHandleRaw(
                    handle,
                    FileRenameInformation,
                    buffer,
                    (UInt32)size
                )) {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Retained daily lock exact no-replace rename failed"
                    );
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static string GetFinalPath(SafeFileHandle handle) {
            StringBuilder path = new StringBuilder(32768);
            UInt32 length = GetFinalPathNameByHandleW(
                handle,
                path,
                (UInt32)path.Capacity,
                0
            );
            if (length == 0 || length >= path.Capacity) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Retained daily lock path lookup failed"
                );
            }
            string value = path.ToString();
            if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) {
                return @"\\" + value.Substring(8);
            }
            if (value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) {
                return value.Substring(4);
            }
            return value;
        }
    }
}
