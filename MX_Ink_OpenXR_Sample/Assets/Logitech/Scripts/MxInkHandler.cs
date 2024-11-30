using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;

public class MxInkHandler : StylusHandler
{
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

    // List to keep track of drawn lines or points
    private List<GameObject> _drawnElements = new List<GameObject>();

    private void Awake()
    {
        _tipActionRef.action.Enable();
        _grabActionRef.action.Enable();
        _optionActionRef.action.Enable();
        _middleActionRef.action.Enable();

        InputSystem.onDeviceChange += OnDeviceChange;

        CreateResetButton();
        CreateNoiseCube();
    }

    private void OnDestroy()
    {
        InputSystem.onDeviceChange -= OnDeviceChange;
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

        // Check for reset button press with front button
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
        }
    }

    private void PlacePaperAtCurrentPosition()
    {
        // Create a plane to represent the paper
        _paper = GameObject.CreatePrimitive(PrimitiveType.Plane);
        Vector3 paperPosition = transform.position + Vector3.down * 0.01f; // Slightly below the tip
        _paper.transform.position = paperPosition;
        _paper.transform.rotation = Quaternion.Euler(0, transform.eulerAngles.y, 0);
        // Scale the paper to a reasonable size
        _paper.transform.localScale = new Vector3(0.1f, 1f, 0.1f);
        // Set the paper's material color to white
        _paper.GetComponent<Renderer>().material.color = Color.white;
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

    private void ResetPaper()
    {
        if (_paper != null)
        {
            Destroy(_paper);
            _paper = null;
            _paperPlaced = false;
        }
        // Destroy all drawn elements
        foreach (GameObject element in _drawnElements)
        {
            Destroy(element);
        }
        _drawnElements.Clear();
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

        // Apply random noise if noise effect is active
        if (_noiseActive)
        {
            float noiseAmount = 0.005f; // Adjust the noise amplitude as needed
            drawPosition += new Vector3(
                Random.Range(-noiseAmount, noiseAmount),
                Random.Range(-noiseAmount, noiseAmount),
                Random.Range(-noiseAmount, noiseAmount)
            );
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
}
