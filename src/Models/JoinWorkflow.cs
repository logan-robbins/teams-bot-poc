namespace TeamsMediaBot.Models;

/// <summary>
/// Canonical join mode names used by API requests and configuration.
/// </summary>
public static class JoinModeNames
{
    /// <summary>
    /// Teams policy-based mode where platform auto-invites the bot.
    /// </summary>
    public const string PolicyAutoInvite = "policy_auto_invite";

    /// <summary>
    /// Explicit invite + Graph join mode.
    /// </summary>
    public const string InviteAndGraphJoin = "invite_and_graph_join";
}

/// <summary>
/// Error codes returned for join workflow failures.
/// </summary>
public static class JoinWorkflowErrorCodes
{
    public const string BotNotInvited = "BOT_NOT_INVITED";
    public const string TenantNotEnabledForMode = "TENANT_NOT_ENABLED_FOR_MODE";
    public const string GraphPermissionMissing = "GRAPH_PERMISSION_MISSING";
    public const string CallJoinFailed7504Or7505 = "CALL_JOIN_FAILED_7504_OR_7505";
}

/// <summary>
/// Join mode configuration with tenant-level overrides.
/// </summary>
public sealed class JoinModeSettings
{
    /// <summary>
    /// Preferred mode for tenants without explicit overrides.
    /// </summary>
    public string PreferredMode { get; init; } = JoinModeNames.PolicyAutoInvite;

    /// <summary>
    /// Whether policy-based auto-invite mode is enabled for the default tenant scope.
    /// </summary>
    public bool PolicyAutoInviteEnabled { get; init; } = false;

    /// <summary>
    /// Whether policy mode should automatically fall back to invite+graph mode when unavailable.
    /// </summary>
    public bool AutoFallbackToInviteAndGraphJoin { get; init; } = true;

    /// <summary>
    /// Whether explicit invite mode requires bot/service-account attendee presence.
    /// </summary>
    public bool RequireBotAttendeeForInviteJoin { get; init; } = true;

    /// <summary>
    /// Per-tenant configuration overrides keyed by tenant id.
    /// </summary>
    public Dictionary<string, TenantJoinModeOverride> TenantOverrides { get; init; } =
        new(StringComparer.OrdinalIgnoreCase);
}

/// <summary>
/// Optional tenant-specific overrides.
/// </summary>
public sealed class TenantJoinModeOverride
{
    public string? PreferredMode { get; init; }
    public bool? PolicyAutoInviteEnabled { get; init; }
    public bool? AutoFallbackToInviteAndGraphJoin { get; init; }
    public bool? RequireBotAttendeeForInviteJoin { get; init; }
}

/// <summary>
/// Command envelope for the join workflow.
/// </summary>
public sealed class JoinMeetingCommand
{
    public required string JoinUrl { get; init; }
    public string DisplayName { get; init; } = "Talestral";
    public bool JoinAsGuest { get; init; }
    public string? RequestedJoinMode { get; init; }
    public string? MeetingId { get; init; }
    public string? OrganizerTenantId { get; init; }
    public DateTime? ScheduledStartUtc { get; init; }
    public bool BotAttendeePresent { get; init; } = true;
}

/// <summary>
/// Result envelope for the join workflow.
/// </summary>
public sealed class JoinMeetingWorkflowResult
{
    public required string SelectedJoinMode { get; init; }
    public required string EffectiveTenantId { get; init; }
    public required string Message { get; init; }
    public string? CallId { get; init; }
    public string? MeetingId { get; init; }
    public bool Deferred { get; init; }
}

/// <summary>
/// Resolved mode decision for a join request.
/// </summary>
public sealed class ResolvedJoinMode
{
    public required string RequestedMode { get; init; }
    public required string SelectedMode { get; init; }
    public required string EffectiveTenantId { get; init; }
    public bool PolicyAutoInviteEnabled { get; init; }
    public bool AutoFallbackToInviteAndGraphJoin { get; init; }
    public bool RequireBotAttendeeForInviteJoin { get; init; }
}

/// <summary>
/// Exception for join workflow failures with machine-readable codes.
/// </summary>
public sealed class JoinWorkflowException : Exception
{
    public JoinWorkflowException(string errorCode, string message, Exception? innerException = null)
        : base(message, innerException)
    {
        ErrorCode = errorCode;
    }

    public string ErrorCode { get; }
}

/// <summary>
/// Utilities for join mode normalization and validation.
/// </summary>
public static class JoinModeParser
{
    public static bool TryNormalize(string? value, out string normalized)
    {
        normalized = string.Empty;
        if (string.IsNullOrWhiteSpace(value))
        {
            return false;
        }

        normalized = value.Trim().ToLowerInvariant();
        return normalized is JoinModeNames.PolicyAutoInvite or JoinModeNames.InviteAndGraphJoin;
    }

    public static string NormalizeOrThrow(string value, string sourceName)
    {
        if (TryNormalize(value, out var normalized))
        {
            return normalized;
        }

        throw new InvalidOperationException(
            $"Unsupported {sourceName} '{value}'. Supported values: " +
            $"'{JoinModeNames.PolicyAutoInvite}', '{JoinModeNames.InviteAndGraphJoin}'.");
    }
}
