using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.Networking; // For UnityWebRequest
using UnityEngine.UI; // For UI components

[Serializable]
public class CalmnessResponse
{
    public int statusCode;
    public string body;
}

[Serializable]
public class CalmnessBody
{
    public float calmness;
}

public class MxInkHandler : StylusHandler
{
    // Status and Calmness messages
    private string _statusMessage = "Status: Ready";
    private string _calmnessMessage = "Calmness: N/A";

    public float paperSizeMultiplier = 0.3f; // Variable to control paper and text size

    public Color active_color = Color.gray;
    public Color double_tap_active_color = Color.cyan;
    public Color default_color = Color.black;

    [SerializeField]
    private InputActionReference _tipActionRef;
    [SerializeField]
    private InputActionReference _grabActionRef; // Front button
    [SerializeField]
    private InputActionReference _optionActionRef; // Erase button
    [SerializeField]
    private InputActionReference _middleActionRef;
    private float _hapticClickDuration = 0.011f;
    private float _hapticClickAmplitude = 1.0f;
    [SerializeField] private GameObject _tip;
    [SerializeField] private GameObject _cluster_front;
    [SerializeField] private GameObject _cluster_middle;
    [SerializeField] private GameObject _cluster_back;

    // Variables for paper handling
    private bool _paperPlaced = false;
    private GameObject _paper;

    // Reset button (red cube)
    private GameObject _resetButton;

    // Noise cube (blue/yellow cube)
    private GameObject _noiseCube;
    private bool _noiseActive = false; // Tracks if noise effect is active

    // Connect cube (green cube)
    private GameObject _connectCube;

    // Orange cube
    private GameObject _orangeCube;

    // List to keep track of drawn lines or points
    private List<GameObject> _drawnElements = new List<GameObject>();

    // URL of the Google Docs document
    private string _documentURL = "https://docs.google.com/document/d/1GcIXo94zBM-XJd1duxemQnhjOSwQERW17WEa2QHvS70/export?format=txt";

    // URL for calmness data
    private string _calmnessURL = "https://7me4t4owwi.execute-api.us-west-2.amazonaws.com/prod/calmness_data";

    // Reference to the Text component on the paper
    private Text _paperText;

    // Coroutine for updating the document text
    private Coroutine _updateTextCoroutine;

    // Coroutine for fetching calmness data
    private Coroutine _calmnessCoroutine;

    // Variable to store the current calmness value
    private float _currentCalmness = 1.0f; // Initialized to a high value

    private void Awake()
    {
        _tipActionRef.action.Enable();
        _grabActionRef.action.Enable();
        _optionActionRef.action.Enable();
        _middleActionRef.action.Enable();

        InputSystem.onDeviceChange += OnDeviceChange;

        CreateResetButton();
        CreateNoiseCube();
        CreateConnectCube(); // Added for connect cube
        CreateOrangeCube();  // Added for orange cube
        // Removed CreateStatusText
    }

    private void OnDestroy()
    {
        InputSystem.onDeviceChange -= OnDeviceChange;
        if (_updateTextCoroutine != null)
        {
            StopCoroutine(_updateTextCoroutine);
        }
        if (_calmnessCoroutine != null)
        {
            StopCoroutine(_calmnessCoroutine);
        }
    }

    private void OnDeviceChange(InputDevice device, InputDeviceChange change)
    {
        if (device.name.ToLower().Contains("logitech"))
        {
            switch (change)
            {
                case InputDeviceChange.Disconnected:
                    _tipActionRef.action.Disable();
                    _grabActionRef.action.Disable();
                    _optionActionRef.action.Disable();
                    _middleActionRef.action.Disable();
                    break;
                case InputDeviceChange.Reconnected:
                    _tipActionRef.action.Enable();
                    _grabActionRef.action.Enable();
                    _optionActionRef.action.Enable();
                    _middleActionRef.action.Enable();
                    break;
            }
        }
    }

    void Update()
    {
        _stylus.inkingPose.position = transform.position;
        _stylus.inkingPose.rotation = transform.rotation;
        _stylus.tip_value = _tipActionRef.action.ReadValue<float>();
        _stylus.cluster_middle_value = _middleActionRef.action.ReadValue<float>();
        _stylus.cluster_front_value = _grabActionRef.action.IsPressed(); // Front button
        _stylus.cluster_back_value = _optionActionRef.action.IsPressed(); // Erase button

        _tip.GetComponent<MeshRenderer>().material.color = _stylus.tip_value > 0 ? active_color : default_color;
        _cluster_front.GetComponent<MeshRenderer>().material.color = _stylus.cluster_front_value ? active_color : default_color;
        _cluster_middle.GetComponent<MeshRenderer>().material.color = _stylus.cluster_middle_value > 0 ? active_color : default_color;
        _cluster_back.GetComponent<MeshRenderer>().material.color = _stylus.cluster_back_value ? active_color : default_color;

        // Place paper if tip is pressed and paper hasn't been placed yet
        if (_stylus.tip_value > 0)
        {
            if (!_paperPlaced)
            {
                PlacePaperAtCurrentPosition();
                _paperPlaced = true;

                // Start updating the text
                if (_updateTextCoroutine == null)
                {
                    _updateTextCoroutine = StartCoroutine(UpdateDocumentText());
                }
            }
            else
            {
                // Lower the paper if the tip goes below it
                if (transform.position.y < _paper.transform.position.y)
                {
                    Vector3 paperPosition = _paper.transform.position;
                    paperPosition.y = transform.position.y - 0.01f; // Slightly below the tip
                    _paper.transform.position = paperPosition;
                }
            }

            // Drawing on the paper
            if (!_stylus.cluster_back_value) // Not erasing
            {
                DrawOnPaper();
            }
        }

        // Check for reset, noise, connect, and orange cube interactions with front button
        if (_stylus.cluster_front_value) // Front button is pressed
        {
            // Check for reset button interaction
            if (_resetButton != null)
            {
                float distanceToResetButton = Vector3.Distance(transform.position, _resetButton.transform.position);
                if (distanceToResetButton < 0.05f)
                {
                    ResetPaper();
                }
            }

            // Check for noise cube interaction
            if (_noiseCube != null)
            {
                float distanceToNoiseCube = Vector3.Distance(transform.position, _noiseCube.transform.position);
                if (distanceToNoiseCube < 0.05f)
                {
                    ToggleNoiseEffect();
                }
            }

            // Check for connect cube interaction
            if (_connectCube != null)
            {
                float distanceToConnectCube = Vector3.Distance(transform.position, _connectCube.transform.position);
                if (distanceToConnectCube < 0.05f)
                {
                    StartBluetoothConnection();
                }
            }

            // Check for orange cube interaction
            if (_orangeCube != null)
            {
                float distanceToOrangeCube = Vector3.Distance(transform.position, _orangeCube.transform.position);
                if (distanceToOrangeCube < 0.05f)
                {
                    StartCalmnessFetching();
                }
            }
        }

        // Erase when erase button is pressed
        if (_stylus.cluster_back_value)
        {
            EraseAtPosition();

            // Allow moving the reset button when erase button is pressed near it
            if (_resetButton != null)
            {
                float distanceToResetButton = Vector3.Distance(transform.position, _resetButton.transform.position);
                if (distanceToResetButton < 0.05f)
                {
                    _resetButton.transform.position = transform.position;
                }
            }

            // Allow moving the noise cube when erase button is pressed near it
            if (_noiseCube != null)
            {
                float distanceToNoiseCube = Vector3.Distance(transform.position, _noiseCube.transform.position);
                if (distanceToNoiseCube < 0.05f)
                {
                    _noiseCube.transform.position = transform.position;
                }
            }

            // Allow moving the connect cube when erase button is pressed near it
            if (_connectCube != null)
            {
                float distanceToConnectCube = Vector3.Distance(transform.position, _connectCube.transform.position);
                if (distanceToConnectCube < 0.05f)
                {
                    _connectCube.transform.position = transform.position;
                }
            }

            // Allow moving the orange cube when erase button is pressed near it
            if (_orangeCube != null)
            {
                float distanceToOrangeCube = Vector3.Distance(transform.position, _orangeCube.transform.position);
                if (distanceToOrangeCube < 0.05f)
                {
                    _orangeCube.transform.position = transform.position;
                }
            }
        }
    }

    private void StartBluetoothConnection()
    {
        // Simulate Bluetooth scanning and connecting to Muse2 EEG headband
        // In actual implementation, you need to use a Bluetooth plugin or SDK
        UpdateStatus("Starting Bluetooth scan...");

        // Simulate found devices
        List<string> devices = new List<string> { "Device A", "Device B", "Muse2 EEG Headband", "Device C" };

        // Display choices to user
        ShowDeviceSelectionUI(devices);
    }

    private void ShowDeviceSelectionUI(List<string> devices)
    {
        // Create a Canvas
        GameObject canvasObject = new GameObject("DeviceSelectionCanvas");
        canvasObject.transform.SetParent(transform, false); // Attach to stylus for simplicity
        canvasObject.transform.localPosition = new Vector3(0f, 0f, 0.5f); // Position it in front of the stylus

        Canvas canvas = canvasObject.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.WorldSpace;
        canvas.worldCamera = Camera.main; // Assign the main camera

        CanvasScaler canvasScaler = canvasObject.AddComponent<CanvasScaler>();
        canvasScaler.dynamicPixelsPerUnit = 10f;

        // Set the size of the Canvas
        RectTransform canvasRect = canvas.GetComponent<RectTransform>();
        canvasRect.sizeDelta = new Vector2(300f, 200f); // Adjust size as needed

        // Add a Vertical Layout Group to arrange buttons
        VerticalLayoutGroup layoutGroup = canvasObject.AddComponent<VerticalLayoutGroup>();
        layoutGroup.childAlignment = TextAnchor.MiddleCenter;
        layoutGroup.childForceExpandHeight = false;
        layoutGroup.childControlHeight = true;
        layoutGroup.spacing = 10f; // Add spacing between buttons

        // Add a background image for better visibility
        GameObject background = new GameObject("Background");
        background.transform.SetParent(canvasObject.transform, false);
        Image bgImage = background.AddComponent<Image>();
        bgImage.color = new Color(1f, 1f, 1f, 0.8f); // Semi-transparent white

        RectTransform bgRect = background.GetComponent<RectTransform>();
        bgRect.anchorMin = Vector2.zero;
        bgRect.anchorMax = Vector2.one;
        bgRect.offsetMin = Vector2.zero;
        bgRect.offsetMax = Vector2.zero;

        // For each device, create a button
        foreach (string deviceName in devices)
        {
            GameObject buttonObject = new GameObject(deviceName + "Button");
            buttonObject.transform.SetParent(canvasObject.transform, false);

            Button button = buttonObject.AddComponent<Button>();
            Image image = buttonObject.AddComponent<Image>();
            image.color = Color.white;

            // Add text to the button
            GameObject textObject = new GameObject("Text");
            textObject.transform.SetParent(buttonObject.transform, false);

            Text text = textObject.AddComponent<Text>();
            text.font = Resources.GetBuiltinResource<Font>("Arial.ttf");
            text.fontSize = 14;
            text.alignment = TextAnchor.MiddleCenter;
            text.text = deviceName;
            text.color = Color.black;

            RectTransform textRect = text.GetComponent<RectTransform>();
            textRect.anchorMin = Vector2.zero;
            textRect.anchorMax = Vector2.one;
            textRect.offsetMin = Vector2.zero;
            textRect.offsetMax = Vector2.zero;

            // Adjust the button size
            RectTransform buttonRect = button.GetComponent<RectTransform>();
            buttonRect.sizeDelta = new Vector2(200f, 40f); // Adjust size as needed

            // Add listener to button
            button.onClick.AddListener(() => OnDeviceSelected(deviceName));
        }
    }

    private void OnDeviceSelected(string deviceName)
    {
        // Handle device selection
        UpdateStatus("Selected device: " + deviceName);

        // Simulate connecting to device
        if (deviceName == "Muse2 EEG Headband")
        {
            UpdateStatus("Connecting to Muse2 EEG Headband...");
            // Implement connection logic here
            // Note: Actual connection requires SDK and platform-specific code
        }
        else
        {
            UpdateStatus("Connecting to " + deviceName + "...");
        }

        // Destroy the device selection UI
        Destroy(GameObject.Find("DeviceSelectionCanvas"));
    }

    private void UpdateStatus(string message)
    {
        _statusMessage = $"Status: {message}";
        UpdatePaperText();
    }

    private void UpdateCalmness(float value)
    {
        _currentCalmness = value; // Update the current calmness value
        _calmnessMessage = $"Calmness: {value:F4}";
        UpdatePaperText();
    }

    private void UpdatePaperText()
    {
        if (_paperText != null)
        {
            string documentText = _paperText.text.Contains("\n\n") ?
                _paperText.text.Substring(_paperText.text.IndexOf("\n\n") + 2) :
                "";

            _paperText.text = $"{_statusMessage}\n{_calmnessMessage}\n\n{documentText}";
        }
    }

    private void PlacePaperAtCurrentPosition()
    {
        // Create a plane to represent the paper
        _paper = GameObject.CreatePrimitive(PrimitiveType.Plane);
        Vector3 paperPosition = transform.position + Vector3.down * 0.015f; // Slightly below the tip
        _paper.transform.position = paperPosition;

        // Calculate the angle to rotate the paper so that it faces the user's gaze direction
        Vector3 userForward = Camera.main.transform.forward;
        userForward.y = 0; // Project onto XZ plane
        userForward.Normalize();

        float angle = Vector3.SignedAngle(Vector3.forward, userForward, Vector3.up);

        // Rotate the paper around Y-axis to face the user's direction
        _paper.transform.rotation = Quaternion.Euler(0f, angle, 0f);

        // Scale the paper to a reasonable size
        float paperScale = 0.1f * paperSizeMultiplier;
        _paper.transform.localScale = new Vector3(paperScale, 1f, paperScale);

        // Set the paper's material color to white
        _paper.GetComponent<Renderer>().material.color = Color.white;

        // Create a Canvas as a child of the paper
        GameObject canvasObject = new GameObject("PaperCanvas");
        canvasObject.transform.SetParent(_paper.transform, false);
        canvasObject.transform.localPosition = new Vector3(0f, 0.01f, 0f); // Slightly above the paper surface

        // Rotate the canvas to lie flat on the paper
        canvasObject.transform.localRotation = Quaternion.Euler(90f, 0f, 0f); // Face upwards

        // Adjust scale to match paper size
        canvasObject.transform.localScale = Vector3.one * 0.01f * paperSizeMultiplier;

        Canvas canvas = canvasObject.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.WorldSpace;
        canvas.worldCamera = Camera.main; // Assign the main camera

        // Set the size of the Canvas
        RectTransform canvasRect = canvas.GetComponent<RectTransform>();
        canvasRect.sizeDelta = new Vector2(1000f * paperSizeMultiplier, 1000f * paperSizeMultiplier); // Adjust size as needed

        // Create a Text UI element
        GameObject textObject = new GameObject("PaperText");
        textObject.transform.SetParent(canvasObject.transform, false);
        textObject.transform.localPosition = Vector3.zero;
        textObject.transform.localRotation = Quaternion.identity;
        textObject.transform.localScale = Vector3.one;

        Text text = textObject.AddComponent<Text>();
        text.font = Resources.GetBuiltinResource<Font>("Arial.ttf");
        text.fontSize = Mathf.RoundToInt(20 * paperSizeMultiplier); // Adjust font size

        text.alignment = TextAnchor.MiddleCenter;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        text.color = Color.black;
        text.text = $"{_statusMessage}\n{_calmnessMessage}\n\nLoading...";

        // Adjust the RectTransform
        RectTransform textRect = text.GetComponent<RectTransform>();
        textRect.sizeDelta = canvasRect.sizeDelta; // Match the canvas size
        textRect.localPosition = Vector3.zero;

        // Assign the text reference
        _paperText = text;
    }

    private void CreateResetButton()
    {
        // Create a simple cube as a reset button
        _resetButton = GameObject.CreatePrimitive(PrimitiveType.Cube);
        _resetButton.transform.position = new Vector3(0.1f, 1f, 0); // Position it where it's accessible
        _resetButton.transform.localScale = new Vector3(0.05f, 0.05f, 0.05f);
        _resetButton.GetComponent<Renderer>().material.color = Color.red;
    }

    private void CreateNoiseCube()
    {
        // Create a cube for the noise effect
        _noiseCube = GameObject.CreatePrimitive(PrimitiveType.Cube);
        _noiseCube.transform.position = new Vector3(-0.1f, 1f, 0); // Position it where it's accessible
        _noiseCube.transform.localScale = new Vector3(0.05f, 0.05f, 0.05f);
        _noiseCube.GetComponent<Renderer>().material.color = Color.blue;
    }

    // Method to create the connect cube (green)
    private void CreateConnectCube()
    {
        // Create a cube for the connect effect
        _connectCube = GameObject.CreatePrimitive(PrimitiveType.Cube);
        _connectCube.transform.position = new Vector3(0f, 1f, 0.1f); // Position it where it's accessible
        _connectCube.transform.localScale = new Vector3(0.05f, 0.05f, 0.05f);
        _connectCube.GetComponent<Renderer>().material.color = Color.green;
    }

    // Method to create the orange cube
    private void CreateOrangeCube()
    {
        // Create a cube for the calmness effect
        _orangeCube = GameObject.CreatePrimitive(PrimitiveType.Cube);
        _orangeCube.transform.position = new Vector3(0f, 1f, -0.1f); // Position it where it's accessible
        _orangeCube.transform.localScale = new Vector3(0.05f, 0.05f, 0.05f);
        _orangeCube.GetComponent<Renderer>().material.color = new Color(1f, 0.65f, 0f); // Orange color
    }

    private void ResetPaper()
    {
        if (_paper != null)
        {
            Destroy(_paper);
            _paper = null;
            _paperPlaced = false;

            // Stop updating the text
            if (_updateTextCoroutine != null)
            {
                StopCoroutine(_updateTextCoroutine);
                _updateTextCoroutine = null;
            }

            // Reset status and calmness messages
            _statusMessage = "Status: Ready";
            _calmnessMessage = "Calmness: N/A";
            UpdatePaperText();
        }
        // Destroy all drawn elements
        foreach (GameObject element in _drawnElements)
        {
            Destroy(element);
        }
        _drawnElements.Clear();

        // Stop calmness fetching if active
        if (_calmnessCoroutine != null)
        {
            StopCoroutine(_calmnessCoroutine);
            _calmnessCoroutine = null;
            _calmnessMessage = "Calmness: N/A";
            UpdatePaperText();
        }
    }

    private void ToggleNoiseEffect()
    {
        _noiseActive = !_noiseActive;
        // Change the color of the cube to indicate the noise state
        if (_noiseActive)
        {
            _noiseCube.GetComponent<Renderer>().material.color = Color.yellow;
        }
        else
        {
            _noiseCube.GetComponent<Renderer>().material.color = Color.blue;
        }
    }

    private void DrawOnPaper()
    {
        // Implement simple drawing by instantiating a small sphere at the tip position
        Vector3 drawPosition = transform.position;

        // Apply noise based on new conditions
        if (_noiseActive && _calmnessCoroutine != null && _currentCalmness < 0.45f)
        {
            // 50% chance to add noise
            if (UnityEngine.Random.value < 0.5f)
            {
                float noiseAmount = 0.005f; // Adjust the noise amplitude as needed
                drawPosition += new Vector3(
                    UnityEngine.Random.Range(-noiseAmount, noiseAmount),
                    UnityEngine.Random.Range(-noiseAmount, noiseAmount),
                    UnityEngine.Random.Range(-noiseAmount, noiseAmount)
                );
            }
            // Else, no noise added
        }

        GameObject dot = GameObject.CreatePrimitive(PrimitiveType.Sphere);
        dot.transform.position = drawPosition;
        dot.transform.localScale = Vector3.one * 0.005f;
        dot.GetComponent<Renderer>().material.color = Color.black;
        _drawnElements.Add(dot);
    }

    private void EraseAtPosition()
    {
        RaycastHit hit;
        if (Physics.Raycast(transform.position, -transform.up, out hit, 0.1f))
        {
            GameObject hitObject = hit.collider.gameObject;
            if (_drawnElements.Contains(hitObject))
            {
                _drawnElements.Remove(hitObject);
                Destroy(hitObject);
            }
        }
    }

    public void TriggerHapticPulse(float amplitude, float duration)
    {
        var device = UnityEngine.XR.InputDevices.GetDeviceAtXRNode(_stylus.isOnRightHand ? UnityEngine.XR.XRNode.RightHand : UnityEngine.XR.XRNode.LeftHand);
        device.SendHapticImpulse(0, amplitude, duration);
    }

    public void TriggerHapticClick()
    {
        TriggerHapticPulse(_hapticClickAmplitude, _hapticClickDuration);
    }

    public override bool CanDraw()
    {
        return true;
    }

    private IEnumerator UpdateDocumentText()
    {
        while (true)
        {
            using (UnityWebRequest www = UnityWebRequest.Get(_documentURL))
            {
                yield return www.SendWebRequest();

                if (www.result == UnityWebRequest.Result.ConnectionError || www.result == UnityWebRequest.Result.ProtocolError)
                {
                    Debug.LogError("Error fetching document text: " + www.error);
                    if (_paperText != null)
                    {
                        // Update status to indicate error and clear document text
                        _paperText.text = $"{_statusMessage}\n{_calmnessMessage}\n\nError loading document.";
                    }
                }
                else
                {
                    string documentText = www.downloadHandler.text;
                    if (_paperText != null)
                    {
                        _paperText.text = $"{_statusMessage}\n{_calmnessMessage}\n\n{documentText}";
                    }
                }
            }

            // Wait for some time before fetching again
            yield return new WaitForSeconds(1f); // Update every 1 second
        }
    }

    private void StartCalmnessFetching()
    {
        if (_calmnessCoroutine == null)
        {
            _calmnessCoroutine = StartCoroutine(FetchCalmnessData());
        }
    }

    private IEnumerator FetchCalmnessData()
    {
        while (true)
        {
            using (UnityWebRequest www = UnityWebRequest.Get(_calmnessURL))
            {
                yield return www.SendWebRequest();

                if (www.result == UnityWebRequest.Result.ConnectionError || www.result == UnityWebRequest.Result.ProtocolError)
                {
                    Debug.LogError("Error fetching calmness data: " + www.error);
                    UpdateCalmnessMessage("Error");
                }
                else
                {
                    string responseText = www.downloadHandler.text;
                    try
                    {
                        CalmnessResponse response = JsonUtility.FromJson<CalmnessResponse>(responseText);
                        if (response != null && !string.IsNullOrEmpty(response.body))
                        {
                            CalmnessBody body = JsonUtility.FromJson<CalmnessBody>(response.body);
                            if (body != null)
                            {
                                UpdateCalmness(body.calmness);
                            }
                            else
                            {
                                UpdateCalmnessMessage("Invalid Data");
                            }
                        }
                        else
                        {
                            UpdateCalmnessMessage("Invalid Response");
                        }
                    }
                    catch (Exception e)
                    {
                        Debug.LogError("Error parsing calmness data: " + e.Message);
                        UpdateCalmnessMessage("Parse Error");
                    }
                }
            }

            // Wait for 10 milliseconds before the next request
            yield return new WaitForSeconds(0.01f); // 10 ms
        }
    }

    private void UpdateCalmnessMessage(string message)
    {
        _calmnessMessage = $"Calmness: {message}";
        UpdatePaperText();
    }
}
