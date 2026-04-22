using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public sealed class GraphNotificationCrypto
{
    private readonly MeetingChatConfiguration _config;
    private readonly ILogger<GraphNotificationCrypto> _logger;
    private readonly Lazy<X509Certificate2?> _certificate;

    public GraphNotificationCrypto(
        MeetingChatConfiguration config,
        ILogger<GraphNotificationCrypto> logger)
    {
        _config = config;
        _logger = logger;
        _certificate = new Lazy<X509Certificate2?>(LoadCertificate);
    }

    public bool IsEnabled => !string.IsNullOrWhiteSpace(_config.GraphSubscriptionEncryptionCertPath);

    public string? EncryptionCertificateId
    {
        get
        {
            if (!IsEnabled)
            {
                return null;
            }

            return !string.IsNullOrWhiteSpace(_config.GraphSubscriptionEncryptionCertId)
                ? _config.GraphSubscriptionEncryptionCertId
                : _certificate.Value?.Thumbprint;
        }
    }

    public string? GetPublicCertificateBase64()
    {
        if (!IsEnabled)
        {
            return null;
        }

        var certificate = _certificate.Value
            ?? throw new InvalidOperationException("Graph encryption certificate could not be loaded.");

        return Convert.ToBase64String(certificate.Export(X509ContentType.Cert));
    }

    public JsonDocument DecryptPayload(GraphEncryptedContent encryptedContent)
    {
        ArgumentNullException.ThrowIfNull(encryptedContent);

        var certificate = _certificate.Value
            ?? throw new InvalidOperationException("Graph encryption certificate could not be loaded.");

        using var rsa = certificate.GetRSAPrivateKey();
        if (rsa is null)
        {
            throw new InvalidOperationException("Graph encryption certificate does not include a private key.");
        }

        var symmetricKey = rsa.Decrypt(
            Convert.FromBase64String(encryptedContent.DataKey),
            RSAEncryptionPadding.OaepSHA1);

        var cipherBytes = Convert.FromBase64String(encryptedContent.Data);
        var signatureBytes = Convert.FromBase64String(encryptedContent.DataSignature);

        using (var hmac = new HMACSHA256(symmetricKey))
        {
            var computed = hmac.ComputeHash(cipherBytes);
            if (!CryptographicOperations.FixedTimeEquals(computed, signatureBytes))
            {
                throw new CryptographicException("Graph notification signature validation failed.");
            }
        }

        using var aes = Aes.Create();
        aes.Key = symmetricKey;
        aes.IV = symmetricKey[..16];
        aes.Mode = CipherMode.CBC;
        aes.Padding = PaddingMode.PKCS7;

        using var decryptor = aes.CreateDecryptor();
        var plaintextBytes = decryptor.TransformFinalBlock(cipherBytes, 0, cipherBytes.Length);
        var plaintext = Encoding.UTF8.GetString(plaintextBytes);

        return JsonDocument.Parse(plaintext);
    }

    private X509Certificate2? LoadCertificate()
    {
        if (!IsEnabled)
        {
            return null;
        }

        var path = Path.GetFullPath(_config.GraphSubscriptionEncryptionCertPath!);
        if (!File.Exists(path))
        {
            throw new InvalidOperationException(
                $"Graph subscription encryption certificate not found at '{path}'.");
        }

        _logger.LogInformation("Loading Graph encryption certificate from {Path}", path);

        var extension = Path.GetExtension(path);
        return extension.Equals(".pfx", StringComparison.OrdinalIgnoreCase)
            || extension.Equals(".p12", StringComparison.OrdinalIgnoreCase)
                ? new X509Certificate2(
                    path,
                    _config.GraphSubscriptionEncryptionCertPassword,
                    X509KeyStorageFlags.Exportable | X509KeyStorageFlags.EphemeralKeySet)
                : new X509Certificate2(path);
    }
}
