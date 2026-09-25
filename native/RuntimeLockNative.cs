// Precompiled Dawnstrike support surface formerly embedded in scripts/runtime_activation_lock.ps1.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Locking {
    [StructLayout(LayoutKind.Sequential)]
    public struct RuntimeFileDispositionInfo {
        [MarshalAs(UnmanagedType.Bool)]
        public bool DeleteFile;
    }

    public static class RuntimeLockNative {
        private const UInt32 GenericRead = 0x80000000;
        private const UInt32 GenericWrite = 0x40000000;
        private const UInt32 DeleteAccess = 0x00010000;
        private const UInt32 FileReadAttributes = 0x00000080;
        private const UInt32 FileShareRead = 0x00000001;
        private const UInt32 FileShareWrite = 0x00000002;
        private const UInt32 CreateNew = 1;
        private const UInt32 OpenExisting = 3;
        private const UInt32 FileAttributeNormal = 0x00000080;
        private const UInt32 FileFlagBackupSemantics = 0x02000000;
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

        private static SafeFileHandle Open(
            string path,
            UInt32 access,
            UInt32 share,
            UInt32 disposition,
            UInt32 flags,
            string label
        ) {
            SafeFileHandle handle = CreateFileW(
                path, access, share, IntPtr.Zero, disposition, flags, IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, label);
            }
            return handle;
        }

        public static SafeFileHandle CreateNewRetained(string path) {
            return Open(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                CreateNew,
                FileAttributeNormal,
                "Retained runtime lock creation failed"
            );
        }

        public static SafeFileHandle OpenExistingRetained(string path) {
            return Open(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                OpenExisting,
                FileAttributeNormal | FileFlagOpenReparsePoint,
                "Retained runtime lock open failed"
            );
        }

        public static SafeFileHandle OpenDirectoryRetained(string path) {
            return Open(
                path,
                FileReadAttributes,
                FileShareRead | FileShareWrite,
                OpenExisting,
                FileFlagBackupSemantics | FileFlagOpenReparsePoint,
                "Retained runtime lock root open failed"
            );
        }

        public static void MarkDelete(SafeFileHandle handle) {
            RuntimeFileDispositionInfo disposition = new RuntimeFileDispositionInfo();
            disposition.DeleteFile = true;
            int size = Marshal.SizeOf(typeof(RuntimeFileDispositionInfo));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(disposition, buffer, false);
                if (!SetFileInformationByHandle(
                    handle, FileDispositionInformation, buffer, (UInt32)size
                )) {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Retained runtime lock exact deletion failed"
                    );
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static void RenameNoReplace(SafeFileHandle handle, string destination) {
            byte[] name = Encoding.Unicode.GetBytes(destination);
            int rootOffset = IntPtr.Size;
            int lengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = lengthOffset + 4;
            // FileNameLength excludes the terminator, but Windows still reads
            // the variable tail as a Unicode string on supported Desktop
            // builds.  Reserve and zero an explicit terminator so the rename
            // cannot acquire allocator-tail bytes in its directory entry.
            int size = nameOffset + name.Length + 2;
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                for (int index = 0; index < size; index++) Marshal.WriteByte(buffer, index, 0);
                Marshal.WriteByte(buffer, 0, 0);
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, lengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!SetFileInformationByHandle(
                    handle, FileRenameInformation, buffer, (UInt32)size
                )) {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Retained runtime lock exact rename failed"
                    );
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static string GetFinalPath(SafeFileHandle handle) {
            StringBuilder path = new StringBuilder(32768);
            UInt32 length = GetFinalPathNameByHandleW(
                handle, path, (UInt32)path.Capacity, 0
            );
            if (length == 0 || length >= path.Capacity) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Retained runtime lock path lookup failed"
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
