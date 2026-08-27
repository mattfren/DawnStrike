if (-not ("Dawnstrike.Native.JobProcessRunner" -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Native
{
    public sealed class JobProcessResult
    {
        public string Stdout { get; set; }
        public string Stderr { get; set; }
        public int ExitCode { get; set; }
        public int ActiveJobMembersAfterCleanup { get; set; }
    }

    public static class JobProcessRunner
    {
        private const uint CREATE_SUSPENDED = 0x00000004;
        private const uint CREATE_NO_WINDOW = 0x08000000;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const uint STARTF_USESTDHANDLES = 0x00000100;
        private const uint HANDLE_FLAG_INHERIT = 0x00000001;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const int JobObjectBasicAccountingInformation = 1;
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint WAIT_OBJECT_0 = 0x00000000;
        private const uint WAIT_TIMEOUT = 0x00000102;
        private const uint WAIT_FAILED = 0xFFFFFFFF;
        private const uint JOB_TERMINATION_EXIT_CODE = 0xD15EA5ED;

        [StructLayout(LayoutKind.Sequential)]
        private struct SECURITY_ATTRIBUTES
        {
            public int nLength;
            public IntPtr lpSecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool bInheritHandle;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        {
            public long TotalUserTime;
            public long TotalKernelTime;
            public long ThisPeriodTotalUserTime;
            public long ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount;
            public uint TotalProcesses;
            public uint ActiveProcesses;
            public uint TotalTerminatedProcesses;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(
            IntPtr lpJobAttributes,
            string lpName
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr hJob,
            int JobObjectInfoClass,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryInformationJobObject(
            IntPtr hJob,
            int JobObjectInfoClass,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength,
            IntPtr lpReturnLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(
            IntPtr hJob,
            IntPtr hProcess
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreatePipe(
            out IntPtr hReadPipe,
            out IntPtr hWritePipe,
            ref SECURITY_ATTRIBUTES lpPipeAttributes,
            uint nSize
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetHandleInformation(
            IntPtr hObject,
            uint dwMask,
            uint dwFlags
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcessW(
            string lpApplicationName,
            StringBuilder lpCommandLine,
            IntPtr lpProcessAttributes,
            IntPtr lpThreadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool bInheritHandles,
            uint dwCreationFlags,
            IntPtr lpEnvironment,
            string lpCurrentDirectory,
            ref STARTUPINFO lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr hThread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr hObject);

        public static string QuoteArgument(string argument)
        {
            if (argument == null)
            {
                throw new ArgumentNullException("argument");
            }
            bool needsQuotes = argument.Length == 0;
            for (int index = 0; index < argument.Length && !needsQuotes; index += 1)
            {
                needsQuotes = char.IsWhiteSpace(argument[index]) || argument[index] == '"';
            }
            if (!needsQuotes)
            {
                return argument;
            }

            StringBuilder encoded = new StringBuilder();
            encoded.Append('"');
            int backslashes = 0;
            foreach (char value in argument)
            {
                if (value == '\\')
                {
                    backslashes += 1;
                    continue;
                }
                if (value == '"')
                {
                    encoded.Append('\\', backslashes * 2 + 1);
                    encoded.Append('"');
                    backslashes = 0;
                    continue;
                }
                encoded.Append('\\', backslashes);
                encoded.Append(value);
                backslashes = 0;
            }
            encoded.Append('\\', backslashes * 2);
            encoded.Append('"');
            return encoded.ToString();
        }

        public static JobProcessResult Run(
            string filePath,
            string[] arguments,
            string workingDirectory,
            string label,
            int timeoutMilliseconds,
            int outputDrainMilliseconds,
            string[] environmentOverrides
        )
        {
            if (String.IsNullOrWhiteSpace(filePath))
            {
                throw new ArgumentException("filePath is required.");
            }
            if (String.IsNullOrWhiteSpace(workingDirectory))
            {
                throw new ArgumentException("workingDirectory is required.");
            }
            if (timeoutMilliseconds < 1 || outputDrainMilliseconds < 1)
            {
                throw new ArgumentOutOfRangeException("timeouts must be positive.");
            }

            IntPtr job = IntPtr.Zero;
            IntPtr stdoutRead = IntPtr.Zero;
            IntPtr stdoutWrite = IntPtr.Zero;
            IntPtr stderrRead = IntPtr.Zero;
            IntPtr stderrWrite = IntPtr.Zero;
            IntPtr stdinRead = IntPtr.Zero;
            IntPtr stdinWrite = IntPtr.Zero;
            IntPtr environment = IntPtr.Zero;
            PROCESS_INFORMATION processInfo = new PROCESS_INFORMATION();
            bool assignedToJob = false;
            bool cleanupConfirmed = false;
            int activeAfterCleanup = -1;
            StreamReader stdoutReader = null;
            StreamReader stderrReader = null;
            Task<string> stdoutTask = null;
            Task<string> stderrTask = null;

            try
            {
                job = CreateJobObject(IntPtr.Zero, null);
                RequireHandle(job, "CreateJobObject");
                ConfigureKillOnClose(job);

                SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
                security.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
                security.bInheritHandle = true;
                Require(CreatePipe(out stdoutRead, out stdoutWrite, ref security, 0), "CreatePipe(stdout)");
                Require(CreatePipe(out stderrRead, out stderrWrite, ref security, 0), "CreatePipe(stderr)");
                Require(CreatePipe(out stdinRead, out stdinWrite, ref security, 0), "CreatePipe(stdin)");
                Require(SetHandleInformation(stdoutRead, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation(stdout)");
                Require(SetHandleInformation(stderrRead, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation(stderr)");
                Require(SetHandleInformation(stdinWrite, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation(stdin)");

                STARTUPINFO startup = new STARTUPINFO();
                startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                startup.dwFlags = (int)STARTF_USESTDHANDLES;
                startup.hStdInput = stdinRead;
                startup.hStdOutput = stdoutWrite;
                startup.hStdError = stderrWrite;

                environment = BuildEnvironmentBlock(environmentOverrides);
                StringBuilder commandLine = BuildCommandLine(filePath, arguments);
                uint flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
                Require(
                    CreateProcessW(
                        filePath,
                        commandLine,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        true,
                        flags,
                        environment,
                        workingDirectory,
                        ref startup,
                        out processInfo
                    ),
                    "CreateProcessW"
                );
                Require(AssignProcessToJobObject(job, processInfo.hProcess), "AssignProcessToJobObject");
                assignedToJob = true;
                CloseOwnedHandle(ref stdoutWrite);
                CloseOwnedHandle(ref stderrWrite);
                CloseOwnedHandle(ref stdinRead);
                CloseOwnedHandle(ref stdinWrite);

                stdoutReader = ReaderFor(ref stdoutRead);
                stderrReader = ReaderFor(ref stderrRead);
                stdoutTask = stdoutReader.ReadToEndAsync();
                stderrTask = stderrReader.ReadToEndAsync();

                if (ResumeThread(processInfo.hThread) == UInt32.MaxValue)
                {
                    throw NativeFailure("ResumeThread");
                }
                CloseOwnedHandle(ref processInfo.hThread);

                uint wait = WaitForSingleObject(processInfo.hProcess, (uint)timeoutMilliseconds);
                if (wait == WAIT_TIMEOUT)
                {
                    throw new InvalidOperationException(
                        String.Format(
                            "{0} timed out after {1} milliseconds.",
                            label,
                            timeoutMilliseconds
                        )
                    );
                }
                if (wait == WAIT_FAILED)
                {
                    throw NativeFailure("WaitForSingleObject");
                }
                if (wait != WAIT_OBJECT_0)
                {
                    throw new InvalidOperationException(label + " returned an unknown wait state.");
                }

                uint rawExitCode;
                Require(GetExitCodeProcess(processInfo.hProcess, out rawExitCode), "GetExitCodeProcess");
                Task[] outputTasks = new Task[] { stdoutTask, stderrTask };
                if (!Task.WaitAll(outputTasks, outputDrainMilliseconds))
                {
                    throw new InvalidOperationException(label + " output drain timed out after root exit.");
                }

                string stdout = stdoutTask.Result.Trim();
                string stderr = stderrTask.Result.Trim();
                if (rawExitCode != 0)
                {
                    activeAfterCleanup = TerminateOwnedJob(
                        job,
                        processInfo.hProcess,
                        true,
                        outputDrainMilliseconds
                    );
                    cleanupConfirmed = true;
                }
                else
                {
                    activeAfterCleanup = WaitForJobEmpty(job, 1000);
                    if (activeAfterCleanup != 0)
                    {
                        throw new InvalidOperationException(
                            label + " left owned descendants after root exit."
                        );
                    }
                    cleanupConfirmed = true;
                }

                return new JobProcessResult
                {
                    Stdout = stdout,
                    Stderr = stderr,
                    ExitCode = unchecked((int)rawExitCode),
                    ActiveJobMembersAfterCleanup = activeAfterCleanup
                };
            }
            catch (Exception failure)
            {
                Exception cleanupFailure = null;
                if (!cleanupConfirmed)
                {
                    try
                    {
                        activeAfterCleanup = TerminateOwnedJob(
                            job,
                            processInfo.hProcess,
                            assignedToJob,
                            outputDrainMilliseconds
                        );
                        cleanupConfirmed = true;
                    }
                    catch (Exception cleanup)
                    {
                        cleanupFailure = cleanup;
                    }
                }
                string cleanupDetail = cleanupFailure == null
                    ? String.Empty
                    : " cleanup_failure=" + cleanupFailure.Message + ".";
                throw new InvalidOperationException(
                    String.Format(
                        "{0} active_job_members_after_cleanup={1}.{2}",
                        failure.Message,
                        activeAfterCleanup,
                        cleanupDetail
                    ),
                    cleanupFailure == null
                        ? failure
                        : new AggregateException(failure, cleanupFailure)
                );
            }
            finally
            {
                // Close the kill-on-close job first. Even if a later stream
                // disposal were to fail, no owned process can escape this
                // outermost native lifetime boundary.
                CloseOwnedHandle(ref job);
                CloseOwnedHandle(ref processInfo.hThread);
                CloseOwnedHandle(ref processInfo.hProcess);
                CloseOwnedHandle(ref stdoutWrite);
                CloseOwnedHandle(ref stderrWrite);
                CloseOwnedHandle(ref stdinRead);
                CloseOwnedHandle(ref stdinWrite);
                CloseOwnedHandle(ref stdoutRead);
                CloseOwnedHandle(ref stderrRead);
                if (stdoutReader != null) stdoutReader.Dispose();
                if (stderrReader != null) stderrReader.Dispose();
                if (environment != IntPtr.Zero) Marshal.FreeHGlobal(environment);
            }
        }

        private static StringBuilder BuildCommandLine(string filePath, string[] arguments)
        {
            StringBuilder commandLine = new StringBuilder(QuoteArgument(filePath));
            foreach (string argument in arguments ?? new string[0])
            {
                commandLine.Append(' ');
                commandLine.Append(QuoteArgument(argument));
            }
            return commandLine;
        }

        private static IntPtr BuildEnvironmentBlock(string[] overrides)
        {
            SortedDictionary<string, string> values = new SortedDictionary<string, string>(
                StringComparer.OrdinalIgnoreCase
            );
            foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
            {
                values[Convert.ToString(entry.Key)] = Convert.ToString(entry.Value);
            }
            foreach (string pair in overrides ?? new string[0])
            {
                int separator = pair == null ? -1 : pair.IndexOf('=');
                if (separator <= 0)
                {
                    throw new ArgumentException("Environment override must be NAME=VALUE.");
                }
                values[pair.Substring(0, separator)] = pair.Substring(separator + 1);
            }
            StringBuilder block = new StringBuilder();
            foreach (KeyValuePair<string, string> entry in values)
            {
                block.Append(entry.Key);
                block.Append('=');
                block.Append(entry.Value);
                block.Append('\0');
            }
            block.Append('\0');
            return Marshal.StringToHGlobalUni(block.ToString());
        }

        private static StreamReader ReaderFor(ref IntPtr handle)
        {
            SafeFileHandle safe = new SafeFileHandle(handle, true);
            handle = IntPtr.Zero;
            FileStream stream = new FileStream(safe, FileAccess.Read, 4096, false);
            return new StreamReader(stream, Encoding.UTF8, true, 4096, false);
        }

        private static void ConfigureKillOnClose(IntPtr job)
        {
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(limits, pointer, false);
                Require(
                    SetInformationJobObject(
                        job,
                        JobObjectExtendedLimitInformation,
                        pointer,
                        (uint)size
                    ),
                    "SetInformationJobObject"
                );
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        private static int TerminateOwnedJob(
            IntPtr job,
            IntPtr process,
            bool assignedToJob,
            int waitMilliseconds
        )
        {
            if (assignedToJob && job != IntPtr.Zero)
            {
                Require(
                    TerminateJobObject(job, JOB_TERMINATION_EXIT_CODE),
                    "TerminateJobObject"
                );
            }
            if (!assignedToJob && process != IntPtr.Zero)
            {
                uint rootState = WaitForSingleObject(process, 0);
                if (rootState == WAIT_TIMEOUT)
                {
                    // The retained process handle is identity-bound. The root
                    // is still suspended here, so it cannot have descendants.
                    Require(
                        TerminateProcess(process, JOB_TERMINATION_EXIT_CODE),
                        "TerminateProcess(unassigned suspended root)"
                    );
                }
                else if (rootState == WAIT_FAILED)
                {
                    throw NativeFailure("WaitForSingleObject(cleanup preflight)");
                }
            }
            if (process != IntPtr.Zero)
            {
                uint rootWait = WaitForSingleObject(
                    process,
                    (uint)Math.Max(waitMilliseconds, 1000)
                );
                if (rootWait == WAIT_FAILED)
                {
                    throw NativeFailure("WaitForSingleObject(cleanup)");
                }
                if (rootWait != WAIT_OBJECT_0)
                {
                    throw new InvalidOperationException(
                        "Owned root did not terminate during bounded cleanup."
                    );
                }
            }
            int active = job == IntPtr.Zero
                ? 0
                : WaitForJobEmpty(job, Math.Max(waitMilliseconds, 5000));
            if (active != 0)
            {
                throw new InvalidOperationException(
                    String.Format(
                        "Owned job retained {0} active members after termination.",
                        active
                    )
                );
            }
            return active;
        }

        private static int WaitForJobEmpty(IntPtr job, int timeoutMilliseconds)
        {
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
            int active = QueryActiveProcesses(job);
            while (active != 0 && DateTime.UtcNow < deadline)
            {
                Thread.Sleep(25);
                active = QueryActiveProcesses(job);
            }
            return active;
        }

        private static int QueryActiveProcesses(IntPtr job)
        {
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting =
                new JOBOBJECT_BASIC_ACCOUNTING_INFORMATION();
            int size = Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(accounting, pointer, false);
                Require(
                    QueryInformationJobObject(
                        job,
                        JobObjectBasicAccountingInformation,
                        pointer,
                        (uint)size,
                        IntPtr.Zero
                    ),
                    "QueryInformationJobObject"
                );
                accounting = (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                    Marshal.PtrToStructure(
                        pointer,
                        typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                    );
                return checked((int)accounting.ActiveProcesses);
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        private static void Require(bool condition, string operation)
        {
            if (!condition) throw NativeFailure(operation);
        }

        private static void RequireHandle(IntPtr handle, string operation)
        {
            if (handle == IntPtr.Zero || handle == new IntPtr(-1))
            {
                throw NativeFailure(operation);
            }
        }

        private static Exception NativeFailure(string operation)
        {
            return new Win32Exception(Marshal.GetLastWin32Error(), operation + " failed");
        }

        private static void CloseOwnedHandle(ref IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != new IntPtr(-1))
            {
                CloseHandle(handle);
            }
            handle = IntPtr.Zero;
        }
    }
}
'@
}

function Invoke-DawnstrikeJobProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds,
        [Parameter()][ValidateRange(1, 60)][int]$OutputDrainTimeoutSeconds = 5,
        [Parameter()][hashtable]$EnvironmentOverrides = @{}
    )

    $environmentPairs = @(
        $EnvironmentOverrides.GetEnumerator() |
            Sort-Object -Property Key |
            ForEach-Object { "{0}={1}" -f $_.Key, $_.Value }
    )
    return [Dawnstrike.Native.JobProcessRunner]::Run(
        $FilePath,
        @($ArgumentList),
        $WorkingDirectory,
        $Label,
        $TimeoutSeconds * 1000,
        $OutputDrainTimeoutSeconds * 1000,
        $environmentPairs
    )
}
